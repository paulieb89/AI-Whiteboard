---
name: feature
description: Take a whiteboard feature from idea to working code. Plans it with acceptance checks, waits for approval, then loops code, tests, a real run and fixes until the checks hold.
argument-hint: [idea]
disable-model-invocation: true
---

Feature idea: $ARGUMENTS

## 1. Plan

Read the code the idea touches, then reply with:

- What changes, file by file. A new element type or a new operation spans several files, so list every one.
- Acceptance checks. Each is either a test that will pass, or a drawing to make on the dev board plus what the snapshot must show.
- Anything a snapshot cannot prove (animation, feel, keyboard or mouse behaviour) that the user must judge.

Stop and wait for approval. Change the plan if asked.

## 2. Build and check, in rounds

Each round:

1. Write or change the code. Add tests in `tests/test_whiteboard.py` for Python behaviour.
2. Run `.venv/bin/python -m pytest -q` and `ruff check whiteboard tests`.
3. Use the `run-whiteboard` skill: restart the dev board, draw the acceptance drawing, save a snapshot and read it.
4. Compare the results with each acceptance check. List what fails and why.

Start another round for anything that fails. Stop after five rounds, or sooner if two rounds in a row fail the same check for the same reason, and report what is stuck.

Keep the project dependency-free (standard library only). Do not weaken or delete an acceptance check to make it pass; ask instead.

## 3. Report

- Each acceptance check with its evidence: the command and the tail of its output, or the snapshot path and what it shows.
- What is left for the user to judge.
- Files changed. Do not commit.
