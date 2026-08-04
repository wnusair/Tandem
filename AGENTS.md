# Tandem-AIO Agent Behavioral Rules & Coding Guidelines

These rules dictate how code should be written, styled, and committed in this repository.

## 1. Coding Style & Understandability
- Write code in a way that is highly accessible so that even a beginner coder can understand what is going on. Prioritize extreme clarity and readability over clever, dense, or complex one-liners.
- Follow the exact naming conventions already established in the existing codebase. Match the surrounding style perfectly.
- Lay the groundwork first (basic structure, core types, interfaces, or setup) and then build up gradually to the more complex logic. 

## 2. Commenting Rules
- Leave comments in a natural, conversational, human method. Avoid using a bunch of trailing dashes, robotic formatting, or overly verbose structural markers. Write explanations as if you are explaining the code to a teammate.

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
