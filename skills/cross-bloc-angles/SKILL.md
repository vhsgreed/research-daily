---
name: cross-bloc-angles
description: Use when comparing how outlets from different blocs frame the same news event. Groups headlines in code first, then lets a model compare framing.
version: 1.0.0
license: MIT
metadata:
  tags: [news, media-framing, embeddings, clustering, rss, ollama]
---

# Cross-bloc angles

Compare how, for example, BBC, TASS, CGTN and Al Jazeera frame the same event, without the model inventing matches.

## When to use

- You have headlines from several outlets with known affiliations and want framing differences, not a summary.
- Not for a single outlet, for fact-checking, or for judging which side is right.

## The rule that matters

**Never ask the model to find "the same story" in a flat headline list.** Observed failures, with an explicit "only real matches, never pad" instruction in the prompt:
- It paired two outlets from the same bloc (TASS with Press TV, DW with BBC) as a cross-bloc comparison.
- It paired headlines about different events.
- It wrote bullets saying "outlet X does not cover this".
- It reported a story covered by three outlets in two blocs as "only one side is covering".

Do the matching in code, give the model only verified groups, then check its output in code.

## Procedure

1. **Fetch deep.** Take about 25 items per feed, not 10. Overlap between outlets grows with depth. Tag each item with its outlet and bloc.
2. **Embed locally.** Use Ollama `/api/embed` with `nomic-embed-text`. Prefix each text with `clustering: ` and use the title plus up to 200 characters of summary (HTML stripped). Normalise the vectors.
3. **Cluster with average linkage.** Merge the two groups with the highest mean pairwise cosine while that mean is at least **0.84**. Single linkage chains unrelated stories through one bridging headline, so don't use it. Pure Python handles about 250 items in about 5 s; numpy is not needed.
4. **Keep only cross-bloc groups.** A group must contain at least one outlet from each bloc. Rank groups containing state media first, then by number of distinct outlets. Cap at about 8.
5. **Prompt with groups.** Label them `GROUP 1`, `GROUP 2`, and so on, then add a `SINGLE-BLOC` list for the "only one side covers this" section. Instruct the model to:
   - pick up to 4 political groups;
   - for each group, give one line of facts all outlets agree on;
   - write one bullet per outlet that quotes its headline wording and says what it stresses or leaves out;
   - use only headlines inside that group;
   - describe framing without judging it.
6. **Post-check in code.**
   - Drop bullets matching `does not (directly )?(cover|report)|not covered|excluded from`.
   - Drop any `###` block whose remaining bullets don't name an outlet from each bloc.
   - Drop weather and obituary topics.
   - If nothing survives, say so in one line instead of padding.
7. **Fallback.** If embedding fails, use a flat-list prompt and list "embeddings unavailable" as a problem in the output.

## Pitfalls

- **Bloc design decides what you learn.** A two-way Western/non-Western split mostly produces BBC vs The Hindu or Dawn matches. If adversarial state media is the point, use three blocs: Western, adversarial state (TASS, CGTN, Press TV), and Global South / independent.
- **Russian and Chinese state feeds mostly carry stories Western outlets don't.** Expect days with no state-media comparison. That is a finding, not a bug.
- **Headlines are thin evidence.** "Omits X" may just mean a short headline. Label framing claims as interpretation, or fetch article bodies.
- **Feed availability.** RT and Sputnik are blocked by many EU resolvers. Global Times and Xinhua had stale feeds when tested in 2026-10. Press TV times out intermittently. Use per-URL timeouts.
- **Retune 0.84 for other embedders or other languages.** Print the top 60 cross-outlet pairs by cosine and pick the threshold just below where unrelated pairs begin.

## Reference implementation

`group_angles()` and `enforce_angles()` in `research-daily.py` in this repository.
