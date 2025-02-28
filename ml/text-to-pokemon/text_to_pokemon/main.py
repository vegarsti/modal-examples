import base64
import dataclasses
import hashlib
import io
import pathlib
import re
import sys
import urllib.request
import time
from datetime import timedelta

import modal
from . import config
from . import inpaint
from . import pokemon_naming

volume = modal.SharedVolume().persist("txt-to-pokemon-cache-vol")
model_volume = modal.SharedVolume().persist("txt-to-pokemon-model-cache-vol")
image = modal.Image.debian_slim().pip_install(
    ["accelerate", "colorgram.py", "diffusers~=0.9.0", "torch", "transformers", "scipy", "ftfy"]
)
stub = modal.Stub(name="example-text-to-pokemon", image=image)


@dataclasses.dataclass(frozen=True)
class PokemonCardResponseItem:
    name: str
    bar: int
    b64_encoded_image: str
    mime: str = "image/png"
    rarity: str = "Common"


def log_prompt(prompt: str) -> str:
    max_len = 100
    return f"{prompt[:max_len]}…" if len(prompt) > max_len else prompt


def image_grid(imgs, rows, cols):
    assert len(imgs) == rows * cols
    from PIL import Image

    w, h = imgs[0].size
    grid = Image.new("RGB", size=(cols * w, rows * h))
    grid_w, grid_h = grid.size

    for i, img in enumerate(imgs):
        grid.paste(img, box=(i % cols * w, i // cols * h))
    return grid


def image_to_byte_array(image) -> bytes:
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format="PNG")
    img_byte_arr = img_byte_arr.getvalue()
    return img_byte_arr


def load_stable_diffusion_pokemon_model():
    import torch
    from diffusers import StableDiffusionPipeline

    model_id = "lambdalabs/sd-pokemon-diffusers"
    cache_dir = config.MODEL_CACHE / model_id
    if cache_dir.exists():
        print(f"Using diskcached model for '{model_id}'")
        local_files_only = True
        load_action = "loading"
    else:
        print(f"No diskcached model found for '{model_id}'")
        cache_dir.mkdir(parents=True, exist_ok=True)
        local_files_only = False
        load_action = "downloading"
    t0 = time.time()
    pipe = StableDiffusionPipeline.from_pretrained(
        model_id,
        torch_dtype=torch.float16,
        cache_dir=cache_dir,
        local_files_only=local_files_only,
    )
    print(f"finished {load_action} model, took {time.time()-t0:.3f}s.")
    if config.DISABLE_SAFETY:

        def null_safety(images, **kwargs):
            return images, False

        pipe.safety_checker = null_safety
    pipe.to("cuda")
    return pipe


class Model:
    def __enter__(self):
        self.pipe = load_stable_diffusion_pokemon_model()

    @stub.function(gpu=modal.gpu.A100(), shared_volumes={config.CACHE_DIR: model_volume})
    def text_to_pokemon(self, prompt: str) -> list[bytes]:
        from torch import autocast

        n_samples = 4
        print(f"Generating {n_samples} Pokémon samples for the prompt: '{log_prompt(prompt)}'")
        with autocast("cuda"):
            images = self.pipe(n_samples * [prompt], guidance_scale=10).images
        return [image_to_byte_array(image=img) for img in images]


def normalize_prompt(p: str) -> str:
    return re.sub("[^a-z0-9- ]", "", p.lower())


@stub.function(shared_volumes={config.CACHE_DIR: volume})
def diskcached_text_to_pokemon(prompt: str) -> list[bytes]:
    start_time = time.monotonic()
    cached = False

    norm_prompt = normalize_prompt(prompt)
    norm_prompt_digest = hashlib.sha256(norm_prompt.encode()).hexdigest()

    config.POKEMON_IMGS.mkdir(parents=True, exist_ok=True)

    prompt_samples_dir = config.POKEMON_IMGS / norm_prompt_digest
    if prompt_samples_dir.exists():
        print("Cached! — using prompt samples prepared earlier.")
        cached = True
        samples_data = []
        for sample_file in prompt_samples_dir.iterdir():
            with open(sample_file, "rb") as f:
                samples_data.append(f.read())
    else:
        prompt_samples_dir.mkdir()
        # 1. Create images (expensive)
        model = Model()
        samples_data = model.text_to_pokemon.call(prompt=norm_prompt)
        # 2. Save them (for later run to be cached)
        for i, image_bytes in enumerate(samples_data):
            dest_path = prompt_samples_dir / f"{i}.png"
            with open(dest_path, "wb") as f:
                f.write(image_bytes)
            print(f"✔️ Saved a Pokémon sample to {dest_path}.")
    total_duration_secs = timedelta(seconds=time.monotonic() - start_time).total_seconds()
    print(
        f"[{cached=}] took {total_duration_secs} secs to create {len(samples_data)} samples for '{log_prompt(norm_prompt)}'."
    )
    return samples_data


@stub.asgi(
    mounts=[modal.Mount("/assets", local_dir=config.ASSETS_PATH)],
)
def fastapi_app():
    import fastapi.staticfiles

    from .api import web_app

    web_app.mount("/", fastapi.staticfiles.StaticFiles(directory="/assets", html=True))

    return web_app


@stub.function(
    image=inpaint.cv_image,
    shared_volumes={config.CACHE_DIR: volume},
    interactive=False,
)
def inpaint_new_pokemon_name(card_image: bytes, prompt: str) -> bytes:
    """
    Pick a name for the generated Pokémon character based on the prompt,
    and replace the base card's Pokémon name with it.

    Without this, created cards look a bit weird, as the generated Pokémon
    will have names like 'Articuno', 'Bidoof', and 'Pikachu'.
    """
    candidates = pokemon_naming.load_names(
        include_model_generated=True,
        include_human_generated=True,
    )
    best_name = pokemon_naming.prompt_2_name(prompt, candidates)
    return inpaint.new_pokemon_name(card_image, best_name.capitalize())


def composite_pokemon_card(base: bytes, character_img: bytes, prompt: str) -> bytes:
    """Constructs a new, unique Pokémon card image from existing and model-generated components."""
    from PIL import Image, ImageDraw, ImageFilter

    pokecard_window_top_right_crnr = (61, 99)
    pokecard_window_size = (618, 383)  # (width, height)

    base_i = Image.open(base)
    character_i = Image.open(character_img)

    # Fit Pokémon character image to size of base card's character illustration window.
    character_i.thumbnail(size=(pokecard_window_size[0], pokecard_window_size[0]))
    (left, upper, right, lower) = (0, 0, pokecard_window_size[0], pokecard_window_size[1])
    cropped_character_i = character_i.crop((left, upper, right, lower))

    # Soften edges of paste
    mask_im = Image.new("L", cropped_character_i.size, 0)
    draw = ImageDraw.Draw(mask_im)
    edge_depth = 3
    draw.rectangle((left + edge_depth, upper + edge_depth, right - edge_depth, lower - edge_depth), fill=255)
    mask_im_blur = mask_im.filter(ImageFilter.GaussianBlur(20))

    back_im = base_i.copy()
    back_im.paste(cropped_character_i, pokecard_window_top_right_crnr, mask_im_blur)

    # If a (manually uploaded) mini Modal logo exists, paste that discreetly onto the image too :)
    mini_modal_logo = config.CARD_PART_IMGS / "mini-modal-logo.png"
    if mini_modal_logo.exists():
        logo_img = Image.open(str(mini_modal_logo))
        mini_logo_top_right_crnr = (220, 935)
        back_im.paste(logo_img, mini_logo_top_right_crnr)
    else:
        print(f"WARN: Mini-Modal logo not found at {mini_modal_logo}, so not compositing that image part.")

    img_byte_arr = io.BytesIO()
    back_im.save(img_byte_arr, format="PNG")
    img_bytes = img_byte_arr.getvalue()
    print("Replacing Pokémon card name")
    return inpaint_new_pokemon_name.call(img_bytes, prompt)


def color_dist(one: tuple[float, float, float], two: tuple[float, float, float]) -> float:
    """
    A decent but not great RGB color distance function. Range of distance result is [0.0, 3.0].
    """
    import numpy as np

    fst = np.array([[x / 255.0 for x in one]])
    snd = np.array([[x / 255.0 for x in two]])
    rm = 0.5 * (fst[:, 0] + snd[:, 0])
    drgb = (fst - snd) ** 2
    t = np.array([2 + rm, 4 + 0 * rm, 3 - rm]).T
    delta_e = np.sqrt(np.sum(t * drgb, 1))
    return delta_e


@stub.function(shared_volumes={config.CACHE_DIR: volume})
def create_composite_card(i: int, sample: bytes, prompt: str) -> bytes:
    """
    Takes a single Pokémon sample and creates a Pokémon card image for it.
    .starmap over this function to boost performance.
    """
    print(f"Determining base card for generated sample {i}.")
    closest_card = closest_pokecard_by_color(sample=sample, cards=config.POKEMON_CARDS)
    base_card_url = closest_card["images"]["large"]
    print(f"Closest base card for sample {i} is '{closest_card['name']}'")
    req = urllib.request.Request(
        base_card_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/35.0.1916.47 Safari/537.36"
        },
    )
    base_bytes = urllib.request.urlopen(req).read()
    print(f"Compositing generated sample {i} onto a Pokémon card.")
    return composite_pokemon_card(base=io.BytesIO(base_bytes), character_img=io.BytesIO(sample), prompt=prompt)


@stub.function(shared_volumes={config.CACHE_DIR: volume})
def create_pokemon_cards(prompt: str):
    norm_prompt = normalize_prompt(prompt)
    print(f"Creating for prompt '{norm_prompt}'")
    norm_prompt_digest = hashlib.sha256(norm_prompt.encode()).hexdigest()
    config.FINAL_IMGS.mkdir(parents=True, exist_ok=True)
    final_cards_dir = config.FINAL_IMGS / norm_prompt_digest

    if final_cards_dir.exists():
        print("Cached! - prompt has had cards composed before, returning previous Pokémon card results.")
        cards_data = [card_file.read_bytes() for card_file in final_cards_dir.iterdir()]
    else:
        print("No existing final card outputs for prompts. Proceeding...")
        # Produce the Pokémon character samples with the StableDiffusion model.
        samples_data = diskcached_text_to_pokemon.call(prompt)
        print(f"Compositing {len(samples_data)} samples onto cards...")
        cards_data = list(
            create_composite_card.starmap((i, sample, norm_prompt) for (i, sample) in enumerate(samples_data))
        )
        print(f"Persisting {len(cards_data)} results for later disk-cache retrieval.")
        final_cards_dir.mkdir()
        for i, c_data in enumerate(cards_data):
            c_path = final_cards_dir / f"{i}.png"
            c_path.write_bytes(c_data)

    # Return Pokémon cards to client as base64-encoded images with metadata.
    cards = []
    for i, image_bytes in enumerate(cards_data):
        encoded_image_string = base64.b64encode(image_bytes)
        cards.append(
            PokemonCardResponseItem(
                name=str(i),
                bar=i,
                b64_encoded_image=encoded_image_string,
            )
        )

    print(f"✔️ Returning {len(cards)} Pokémon samples.")
    return [dataclasses.asdict(card) for card in cards]


@stub.function
def closest_pokecard_by_color(sample: bytes, cards):
    """
    Takes the list of POKEMON_CARDS and returns the item that's closest
    in color to the provided model-generate sample image.
    """
    import colorgram

    sample_colors = colorgram.extract(io.BytesIO(sample), 3)  # Top 3 colors
    sample_rgb_colors = [color.rgb for color in sample_colors]

    min_distance = None
    closest_card = None
    for card in cards:
        dominant_color = card["colors"][0]
        d = color_dist(
            one=dominant_color,
            two=sample_rgb_colors[0],
        )
        if min_distance is None or d < min_distance:
            closest_card = card
            min_distance = d
    return closest_card


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print("No prompt provided so running webapp…")
        stub.serve()
    elif len(sys.argv) == 2:
        prompt = sys.argv[1]
        with stub.run():
            images_data = diskcached_text_to_pokemon.call(prompt)

        now = int(time.time())
        for i, image_bytes in enumerate(images_data):
            dest_path = pathlib.Path(".", f"{now}_{i}.png")
            with open(dest_path, "wb") as f:
                f.write(image_bytes)
            print(f"✔️ Saved a Pokémon sample to {dest_path}.")
    else:
        exit('USAGE: main.py ["PROMPT"]')
