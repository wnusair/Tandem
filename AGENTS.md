# Tandem-AIO Agent Behavioral Rules & Coding Guidelines

These rules dictate how code should be written, styled, and committed in this repository.

## 1. Coding Style & Understandability
- Write code in a way that is highly accessible so that even a beginner coder can understand what is going on. Prioritize extreme clarity and readability over clever, dense, or complex one-liners.
- Follow the exact naming conventions already established in the existing codebase. Match the surrounding style perfectly.
- Lay the groundwork first (basic structure, core types, interfaces, or setup) and then build up gradually to the more complex logic. 

## 2. Commenting Rules
- Comment only where the code cannot speak for itself. Explain *why* something is the way it is, never *what* the line below does. If the code is clear, leave it alone.
- Keep the wording plain and human, the way you'd write it in a code review. Natural tone, not a conversation with the reader: no narrating your own thought process, no telling the story of the bug you fixed, no asides.
- One or two lines. If it needs a paragraph, the code probably needs the rework instead.
- No decorative formatting: no banner separators, no rows of dashes, no numbered step headers. A comment is a sentence, not a section marker.
- Public functions and types get a short doc comment saying what they're for. Private helpers usually don't need one.
- Keep them true. A stale comment is worse than none, so if you change the code, change or delete the comment with it.
- Follow the rules of the Ponytail plugin if applicable.

## 3. Git Workflow
- Periodically stage changes (`git add`) and commit every hundred lines or so (or after small, logical milestones). Do not build up massive changes for a single commit.
- Write commit messages that sound human (e.g., "added basic flask app with tandem task", "fix dummy wasm fallback"). Keep them concise, simple, and casual. Nothing fancy or too long, and definitely no robotic, overly detailed automated formats.
- Never push, always only add and commit. NEVER PUSH. This is also the same for documentation.

## 4. Running
- The way tandem is to be installed and ran is through running install.sh and install.bat on the device and having the node and cli all installed and added to path.
- Installation and updating should not be done differently, not a docker container or list of commands, just hte simple install script.

## 5. Working
- Make sure that whenever you are making changes, you actually test them comprehensivly in a manner that is similar to production. dont create a bunch of junk files iwthout deleting them.
- Make sure your changes work via tests similar to production, and then delete any testing files needed.

IMPORTANT, VERY IMPORTANT: NEVER ADD YOURSELF AS A CONTRIBUTOR TO A COMMIT OR COAUTHOR. NEVER.
IMPORTANT, VERY IMPORTANT: NEVER ADD YOURSELF AS A CONTRIBUTOR TO A COMMIT OR COAUTHOR. NEVER.
IMPORTANT, VERY IMPORTANT: NEVER ADD YOURSELF AS A CONTRIBUTOR TO A COMMIT OR COAUTHOR. NEVER.
IMPORTANT, VERY IMPORTANT: NEVER ADD YOURSELF AS A CONTRIBUTOR TO A COMMIT OR COAUTHOR. NEVER.
IMPORTANT, VERY IMPORTANT: NEVER ADD YOURSELF AS A CONTRIBUTOR TO A COMMIT OR COAUTHOR. NEVER.
