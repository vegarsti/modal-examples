<!--
  @component

  The Generator is the main user-interactive component of the application. Handles:

  - user text prompt input and validation
  - backend API interaction
  - loading/progress feedback
  - Pokémon card generation display

-->
<script>
    import { prompts } from "../helpers/prefillPromptData";
    import PrefillPrompt from "./PrefillPrompt.svelte";
    import Card from "./Card.svelte";
    import Pokeball from "./Pokeball.svelte";
    import CardList from "./CardList.svelte";
    import ProgressBar from "./ProgressBar.svelte";

    let filteredPrompts = [];

    const filterPrompts = () => {
        let storageArr = [];
        if (inputValue) {
            prompts.forEach((p) => {
                if (p.toLowerCase().startsWith(inputValue.toLowerCase())) {
                    storageArr = [...storageArr, makeMatchBold(p)];
                }
            });
        }
        filteredPrompts = storageArr;
    };

    /* HANDLING THE INPUT */
    let promptInput; // use with bind:this to focus element
    let inputValue = "";

    $: if (!inputValue) {
        filteredPrompts = [];
        hiLiteIndex = null;
    }

    const setInputVal = (prompt) => {
        inputValue = removeBold(prompt);
        filteredPrompts = [];
        hiLiteIndex = null;
        document.querySelector("#prompt-input").focus();
    };

    $: loading = false;
    $: loadingComplete = false;
    $: error = undefined;
    $: cards = [];
    let poller;

    const setupPoller = (id) => {
        if (poller) {
            clearInterval(poller);
        }
        poller = setInterval(doPoll(id), 2000);
    };

    const doPoll = (id) => async () => {
        const resp = await fetch(`/api/status/${id}`);
        const body = await resp.json();
        if (body.error) {
            error = body.error;
            loading = false;
        } else if (body.finished) {
            loadingComplete = true;
            clearInterval(poller);
            setTimeout(() => {
                cards = body.cards;
                loading = false;
            }, 500);
        }
    };

    const fetchPokemonCards = async () => {
        loading = true;
        loadingComplete = false;
        const response = await self.fetch(`/api/create?prompt=${inputValue}`);
        if (response.ok) {
            const data = await response.json();
            setupPoller(data.call_id);
        } else {
            throw new Error();
        }
    };

    const handleSubmit = async () => {
        if (inputValue) {
            console.log(`${inputValue} is submitted!`);
            await fetchPokemonCards();
        } else {
            alert("You didn't type anything.");
        }
    };

    const makeMatchBold = (str) => {
        // replace part of (prompt name === inputValue) with strong tags
        let matched = str.substring(0, inputValue.length);
        let makeBold = `<strong>${matched}</strong>`;
        let boldedMatch = str.replace(matched, makeBold);
        return boldedMatch;
    };

    const removeBold = (str) => {
        //replace < and > all characters between
        return str.replace(/<(.)*?>/g, "");
    };

    let hiLiteIndex = null;
    $: hiLitedPrompt = filteredPrompts[hiLiteIndex];

    const navigateList = (e) => {
        if (
            e.key === "ArrowDown" &&
            hiLiteIndex <= filteredPrompts.length - 1
        ) {
            hiLiteIndex === null ? (hiLiteIndex = 0) : (hiLiteIndex += 1);
        } else if (e.key === "ArrowUp" && hiLiteIndex !== null) {
            hiLiteIndex === 0
                ? (hiLiteIndex = filteredPrompts.length - 1)
                : (hiLiteIndex -= 1);
        } else if (e.key === "Enter") {
            setInputVal(filteredPrompts[hiLiteIndex]);
        } else {
            return;
        }
    };
</script>

<svelte:window on:keydown={navigateList} />

<div class="promptbox">
    <div class="form-wrapper">
        <form autocomplete="off" on:submit|preventDefault={handleSubmit}>
            <div class="autocomplete shadow-lg">
                <input
                    id="prompt-input"
                    type="text"
                    placeholder="Enter a prompt"
                    bind:this={promptInput}
                    bind:value={inputValue}
                    on:input={filterPrompts}
                />
                <!-- FILTERED LIST OF Prompts -->
                {#if filteredPrompts?.length > 0}
                    <ul id="autocomplete-items-list">
                        {#each filteredPrompts as prompt, i}
                            <PrefillPrompt
                                itemLabel={prompt}
                                highlighted={i === hiLiteIndex}
                                on:click={() => setInputVal(prompt)}
                            />
                        {/each}
                    </ul>
                {/if}
            </div>

            <div>
                <button
                    class="promptbutton shadow-lg"
                    type="submit"
                    disabled={loading}
                >
                    <Pokeball />
                    <span>Create</span>
                </button>
            </div>
        </form>
    </div>

    {#if loading}
        <ProgressBar finished={loadingComplete} />
        <CardList>
            {#each Array(4) as _, i}
                <Card
                    name={"placeholder"}
                    img={"../img/whos-that-pokemon.png"}
                    number={"-1"}
                    supertype={"Pokémon"}
                    subtypes={["Basic"]}
                    rarity="Uncommon"
                />
            {/each}
        </CardList>
    {:else if cards}
        <CardList>
            {#each cards as { name, bar, mime, b64_encoded_image, rarity } (name)}
                <Card
                    {name}
                    img={`data:${mime};base64,${b64_encoded_image}`}
                    number={`${bar}`}
                    supertype={"Pokémon"}
                    subtypes={["Basic"]}
                    {rarity}
                />
            {/each}
        </CardList>
    {:else if error}
        <p style="color: red">{error.message}</p>
    {/if}
</div>

<style>
    div.autocomplete {
        /*the container must be positioned relative:*/
        position: relative;
        display: inline-block;
        width: 35em;
        height: 65px;
    }

    form {
        display: flex;
        align-items: flex-start;
    }

    .form-wrapper {
        display: flex;
        justify-content: center;
    }

    input {
        border: 1px solid transparent;
        border-radius: 1em;
        background-color: #f1f1f1;
        padding: 1em;
        font-size: 1.25em;
        margin: 0;
    }
    input[type="text"] {
        background-color: #f1f1f1;
        width: 100%;
        margin-right: 1em;
    }
    button[type="submit"] {
        /* I don't yet know why these need !important */
        background: DodgerBlue !important;
        margin-left: 0.5em !important;
        border-radius: 1em;
        border: none;
        color: #fff;
        cursor: pointer;
        background: none;
        font-size: 1.25em;
        margin: 0;
    }
    button[type="submit"]:hover {
        background: rgb(30, 68, 240) !important;
    }

    #autocomplete-items-list {
        position: relative;
        margin: 0;
        padding: 0;
        margin-top: 0.5em;
        top: 0;
        width: 297px;
        border: 1px solid #ddd;
        background-color: #ddd;
        border-radius: 1em;
        width: 100%;
    }

    .promptbox {
        margin-top: 5em;
        display: flex;
        justify-content: center;
        flex-direction: column;
    }

    .shadow-lg {
        --tw-shadow: 0 10px 15px -3px rgb(0 0 0 / 0.1),
            0 4px 6px -4px rgb(0 0 0 / 0.1);
        --tw-shadow-colored: 0 10px 15px -3px var(--tw-shadow-color),
            0 4px 6px -4px var(--tw-shadow-color);
        box-shadow: var(--tw-ring-offset-shadow, 0 0 #0000),
            var(--tw-ring-shadow, 0 0 #0000), var(--tw-shadow);
    }

    .promptbutton {
        height: 65px;
        cursor: pointer;
        display: flex;
        flex-direction: row;
        align-items: center;
        padding-left: 0.75em;
        border-radius: 1em;
        margin-left: 1em;
    }

    .promptbutton span {
        font-weight: 700;
        padding: 1em;
    }
</style>
