## What this changes

<!-- One or two sentences. Link the issue if there is one: "Fixes #12". -->

## Why

<!-- The problem it solves, or the improvement it makes. -->

## How I checked it

- [ ] `python3 -m py_compile autoscan/*.py day-trader-skill/skills/*/*.py`
- [ ] `tick --dry-run` for an NSE time and a US time
- [ ] `signal` and `swing` dry runs for the market(s) I touched
- [ ] Both markets still behave as before (unless that is the point of this PR)

<!-- Paste relevant output. For message changes, paste a before and after. -->

## Checklist

- [ ] No secrets, tokens, chat IDs, server addresses or personal emails in the diff
- [ ] No new pip packages (standard library only)
- [ ] Docs updated if behaviour or settings changed
- [ ] Messages still say signals are not a promise or advice
