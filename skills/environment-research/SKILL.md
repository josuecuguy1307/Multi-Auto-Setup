---
name: environment-research
description: Use when the harness has to recommend an environment (a harness, a skill pack, a package, a model) for a kind of work the catalog does not cover, and has to look it up in public sources. It says what a verifiable source is, how a lead is recorded as a hypothesis, which licenses are allowed, and what must never leave the owner's computers.
---

# Researching environments for a kind of work

The catalog (`router-environments`) is the project's own curated list. When no entry is tagged for the kind of work, you may look at public sources and record what you find as a LEAD. A lead is never run, never installed and never the recommended default: it is shown to the owner as an unverified lead, with the page it came from.

## What a verifiable source is
- A page fetched in this run from a host the project allows (`config/doc_sources.yaml`), by `fetch_source`. Code stores the page by hash and returns its capture reference. A fact you remember, a number from a vendor, or a page you did not fetch is not a source, and `record_candidate` refuses it.
- What a lead says about itself (its license, its address, when it was last pushed, whether it is archived) is read BY CODE from the fetched page. You choose which item of a page to record and say why in one sentence; you never type a license, an address or a date.
- A lead found this way is a `hypothesis`. It becomes `tested` or `verified` only by a measurement or a review by the project, never by research.

## Licenses and pins
- Only permissive licenses are recorded: the list in `router-environments/references/policy.json` (`permissive_licenses`). An empty or `NOASSERTION` license is not permissive (`license_missing`); another license is `license_not_permissive`.
- Code also checks, from the fields of the page, that the repository is not archived (`archived`), was pushed recently (`stale`) and has enough stars (`below_min_stars`), and, from its README, that a domain tag appears in it (`no_domain_in_readme`; a package page with no README is checked on its description, `no_domain_in_description`). The numbers (`thresholds` in `references/sources.json`) are data the owner can change; each failing item is shown to you with its reasons (`fails: ...`) and `record_candidate` refuses it with the first one. A lead that passes keeps the checks it passed.
- Nothing from a public source is installed by research: an install needs a pin and a SHA-256 for every file and goes through the setup plan with the owner's approval.

## What you search with, and what never leaves
- Queries are built ONLY from the domain tags of the request (`request.domains`) and the plain words in `references/sources.json` (`query_vocabulary`). You never see the task text or any file content, and you must not try to learn it.
- For a confidential task the tags are the owner's own category names (for example `legal`, `finance`) and nothing else: free words are not allowed to leave.
- Code refuses a query that has a word outside those lists, and a URL that carries a word of the task text. A refusal tells you which word and what is allowed.
- Do not search at all when `known.read` shows a verified environment tagged for the domain: the web is the last rung, not the first.

## Package registries
A PyPI page (`/pypi/<name>/json`) states its license in `license_expression`, in `license` or only as a classifier; code reads an SPDX id from the first two and from a classifier only when it names exactly one license (MIT, ISC); otherwise the package is `license_missing`. An npm package is read through the registry's `/latest` manifest, because the full document of a package can be tens of MB. A package page has no star count or push date, so those checks do not apply; the domain tags must appear in its description.

## How a page is shown to you
A search page comes back as a short list of items: number, name, license, stars, last push, archived and the checks it fails. The search shapes for each domain tag are in `read_known` (`search_shapes`); the code adds the qualifiers (not archived, recent, stars) and allows 4 searches per recommendation; an address already stored is read from its capture and does not count. A repository or package page comes back as one item. Pick items by number.

## The rungs
Known data (`read_known`: tagged catalog entries, leads recorded before, public evidence) first. Public sources (`fetch_source`, `record_candidate`) only when known data names nothing verified. The last rung is the owner: code puts ONE question to him with a recommended default and a runner-up. You never rank, never choose the default and never write to the owner.
