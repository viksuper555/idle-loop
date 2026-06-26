# idle-demo-target

A deliberately tiny Python project used as the target for the `idle-loop` end-to-end demo.

```bash
python -m pytest      # green out of the box
```

`src/calc.py` holds two trivial functions; the seed issues (see `../seed_issues.md`) ask the
loop to extend it. The `src/**` + `tests/**` layout matches idle-loop's default scope allowlist.
