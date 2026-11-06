# Compatibility

The core tool is a Python 3.9+ script. `SKILL.md` uses the Agent Skills format, but each product has a different privacy boundary. A "local" command tool does not necessarily mean the AI model itself runs locally.

| Environment | Experience | Setup |
| --- | --- | --- |
| Hermes Agent | Local command workflow | For prerelease testing, open the exact supplied project folder, confirm `.hermes/skills/snapchat-memories-saver/SKILL.md` is present, run `hermes skills trust .`, then start a new Hermes session in that project and verify the skill is listed. After v0.2.0 is published and verified, use only its immutable tagged `SKILL.md` URL. Do not install mutable `main`. |
| Claude Code | Local command workflow | Copy this folder into a Claude skills directory or point Claude Code at the repository. The audit script runs locally, while model requests may still use Anthropic's service. |
| Claude Cowork or Claude.ai Skills | Cloud/workspace boundary | Use as a walkthrough unless the user has reviewed and accepted that workspace's data handling. Do not upload private exports by default. |
| Grok CLI | Local command workflow | Copy this folder to `.grok/skills/snapchat-memories-saver` in a project, or `~/.grok/skills/snapchat-memories-saver` globally. The script runs locally, while model requests may still use xAI's service. |
| Grok Bot | Bot/workspace boundary | Do not assume the bot has local-only file access. Use it as a guide unless its connector and data boundary are verified. |
| ChatGPT Agent Skills | Cloud or connected-workspace boundary | Availability and connectors depend on the account and workspace. Do not assume access to local folders or upload private exports by default. |
| Basic ChatGPT, Claude, or Grok chat | Walkthrough only | Paste [`PROMPT.md`](PROMPT.md). The chat can guide the user but cannot inspect local folders by itself. |

For the safest experience, run `scripts/audit_export.py` on the same computer as the export and use the AI only to guide the steps. Give an AI only the process status, fixed error code, or a reviewed `support.txt` by default. Keep `start_here.html`, `summary.json`, CSV files, filenames, dates, hashes, and media out of model context unless the user explicitly accepts that product's data boundary. The skill never needs Snapchat credentials and should never ask the user to upload private Memories.

Official references:

- Hermes skill installation: <https://hermes-agent.nousresearch.com/docs/user-guide/features/skills>
- Claude Agent Skills: <https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview>
- Grok Agent Skills: <https://docs.x.ai/build/features/skills-plugins-marketplaces>
- Skills in ChatGPT: <https://help.openai.com/en/articles/20001066-skills-in-chatgpt>
