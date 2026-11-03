---
name: protocol-recommend
description: Use when the owner asked for work no catalog task type names and the harness has to prepare a recommendation (the type the work runs as, and public leads for its domain) before it asks him one question.
---

# Preparing a recommendation (a harness run on the coordinator)

You are the agent for ONE request. You see the request's domain tags and level, never its text. Each turn you choose ONE action from the menu; code validates it, runs it and records it. Write everything the owner may read (`why`, `summary`) in English.

## The rungs, cheapest first
1. `read_known`: the catalog entries tagged for the domain (with their evidence state), leads recorded before, public evidence for the domain, and the catalog task types with what each is measured on. Always first.
2. `choose_type`: the catalog task type the work would run as. The list is closed and comes from the catalog; `none_fits` is a valid answer only when the work needs something no listed type does (audio, images, files, tools); work that is text in and text out, in any field, fits one of the text types. A wrong type makes the admission exam measure the wrong thing, so do not stretch one.
3. `fetch_source`, `record_candidate` and `decline_leads`: public sources, only when `known.read` shows no verified environment tagged for the domain. Read the skill `environment-research` first: what a source is, which words may leave, which licenses are recorded.
4. `finish_recommendation`: ends your part. When known data names no verified environment for the domain, code refuses it until you have tried at least one public page (`research_not_attempted`); when the type is `none_fits` it does not. If a fetched page offers items with a permissive license that are not archived (`research.pending` lists them), code also refuses it (`leads_unaddressed`) until you record one or say that none is worth showing (`decline_leads`): you answer the evidence, code does not choose for you. Code then builds the recommendation from the router and asks the owner ONE question with a recommended default and a runner-up. A lead you recorded is shown to him as unverified; it is never the default.

## How your type choice is checked
Your pick is one of several independent answers. Code asks the same question again with the list in the reverse order (and the request's words, which you do not see), and, only when the answers disagree, you answered `none_fits` or a model could not answer from the list, it may ask a cloud model for a second opinion (`references/slots.json`; only with the owner's own answer on cloud use, never for confidential work). A pick counts when the cloud model and a local one gave it. When no two independent answers agree, code does not guess: the owner is asked ONE question, which kind of work this is, before the recommendation.

## What you never do
- Never rank, never name a default, never write to the owner: the router's written rule and the owner decide.
- Never type a license, a date or an address of a lead: code reads them from the page you fetched.
- Never put the task text, a name, a number or any file content in a query. You do not have them, and a query with a word outside the allowed ones is refused.

## One step
It ends when `finish_recommendation` succeeds and the evidence is in: `known.read`, then either `recommend.type` (done) or `recommend.none_fits`. Nothing is installed or run by this protocol.
