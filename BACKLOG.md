# Backlog

Small, self-contained tasks the nightly dev agent may pick up (scripts/agents/dev-agent.sh in moistifarius/electrophage).
The agent takes the FIRST unchecked item tagged `ready`, implements it with tests on a branch, and opens a pull request.
It never merges or deploys. Untagged items are ignored, so you decide what it may touch.

Format: `- [ ] ready: <what you want, in one or two sentences — include the acceptance check>`

- [ ] ready: <example — add a unit test covering ... ; done when the new test passes>
- [ ] <an idea that is not ready for the agent yet>
