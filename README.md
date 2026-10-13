# ai-stock-monitor

A cron-friendly Python program that archives multiple RSS/Atom feeds, asks an
OpenAI-compatible LLM to summarize each feed, and produces a rolling news-based note
for configured stocks.

## Setup

Requires Python 3.10 or newer.

### Replace the project path placeholder

Replace every `{{ project_absolute_path }}` in the commands below and in
`crontab.txt` with the actual absolute path to your project directory, including
removing the braces. Run `pwd` from the project directory to find this path.
For example, if `pwd` prints `/home/alice/ai-stock-monitor`, use
`cd /home/alice/ai-stock-monitor` in place of `cd {{ project_absolute_path }}`.
The placeholder is documentation text, not a shell variable; your shell will not
replace it automatically. In your editor, use Find and Replace in `crontab.txt`:
find `{{ project_absolute_path }}`, replace it with the path printed by `pwd`, and
save the file before installing the cron schedule.

Choose either micromamba or Python's built-in `venv` for setup.

### Using micromamba

If needed, install micromamba using the
[official installation instructions](https://mamba.readthedocs.io/en/stable/installation/micromamba-installation.html).
Then create the `stock-monitor` environment used by the cron template and install
the project dependencies. Replace `{{ project_absolute_path }}` in the first
command with your actual project path before running this block:

```bash
cd {{ project_absolute_path }}
export MAMBA_ROOT_PREFIX="${MAMBA_ROOT_PREFIX:-$HOME/micromamba}"
micromamba create -y -n stock-monitor -c conda-forge python=3.12 pip
micromamba run -n stock-monitor python -m pip install -r requirements.txt
cp -n config.example.yml config.yml
```

Edit `config.yml` as described below, then run the project:

```bash
export OPENAI_API_KEY='your-key'
micromamba run -n stock-monitor python stock_news_monitor.py --config config.yml
```

For later runs, use the same `MAMBA_ROOT_PREFIX` and run the last command from the
project directory. `micromamba run` executes inside the environment without shell
activation; see the [micromamba user guide](https://mamba.readthedocs.io/en/stable/user_guide/micromamba.html).

### Using venv

```bash
cd {{ project_absolute_path }}
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yml config.yml
export OPENAI_API_KEY='your-key'
.venv/bin/python stock_news_monitor.py --config config.yml
```

### Configuration

Edit `config.yml` to choose feeds, stocks, model, endpoint, prompts, and the advice
lookback and language. For example, `advice.language: zh-HK` asks the LLM to write the
entire advice in Traditional Chinese appropriate for Hong Kong while preserving ticker
symbols, URLs, and proper nouns. The default language is `en`. `base_url` may be any
service implementing the OpenAI chat-completions API.
The advice prompt evaluates overall and stock-specific news sentiment, its intensity
and confidence, conflicting signals, catalysts, risks, and changes from prior advice.
Do not put the API key in YAML; `api_key_env` names the environment variable to read.
The key is optional: if that variable is unset (or `api_key_env` is omitted and
`OPENAI_API_KEY` is unset), the request is sent without an `Authorization` header.
This is useful for local endpoints such as Ollama-compatible gateways.

## Output

For a feed whose slug is `yahoo-finance`, a run on `2026-08-15` creates:

```text
data/
├── 2026-08-14/
│   ├── yahoo-finance/
│   │   ├── rss.xml
│   │   └── summary.txt
│   └── nasdaq-markets/...
├── 2026-08-15/
│   ├── yahoo-finance/
│   │   ├── rss.xml
│   │   └── summary.txt
│   ├── nasdaq-markets/...
│   └── advice.txt
```

Each date contains one directory per channel, preventing multiple RSS files from
overwriting one another. Historical summaries in the former `data/channel/date`
layout are still read for backward compatibility. One combined advice is generated
from the summaries of every channel
that succeeded in the current run, together with previous combined advice from the
configured lookback window. It is stored as `data/YYYY-MM-DD/advice.txt`. Advice from
the former `data/advices/YYYY-MM-DD.txt` layout is still read as historical context. Existing
daily summaries and advice are retained on repeat runs; pass `--force` to regenerate
them. The raw XML is refreshed on every run. A failure in one feed does not stop the
remaining feeds, but the process exits nonzero if every feed fails or the combined
advice cannot be produced. INFO progress messages, including the active RSS channel,
summarization stage, history loading, and advice generation, are written to standard
output. Warnings and errors are written to standard error.

## Cron

The [`crontab.txt`](crontab.txt) template runs the monitor through the
`stock-monitor` micromamba environment every day at 23:00. It writes logs to
`logs/YYYY-MM-DD/output.log` and `logs/YYYY-MM-DD/error.log`. Before installing it,
open `crontab.txt` and replace every `{{ project_absolute_path }}` with your actual
project path. Replace the placeholder in the installation command too:

```bash
crontab {{ project_absolute_path }}/crontab.txt
```

For example, if your project is at `/home/alice/ai-stock-monitor`, the installation
command becomes `crontab /home/alice/ai-stock-monitor/crontab.txt`.

`output.log` receives normal progress messages. `error.log` receives only warnings
and errors, so it remains empty after a fully successful run.

The included file uses `/usr/local/bin/micromamba` and sets
`MAMBA_ROOT_PREFIX=/mnt/micromamba`. Before installing it, replace the executable
path with the result of `command -v micromamba` and set `MAMBA_ROOT_PREFIX` to the
same absolute path used during setup (run `echo "$MAMBA_ROOT_PREFIX"` in your setup
shell to check). Keep the environment name `stock-monitor` consistent with the
setup commands. Cron uses the machine's local timezone unless its daemon is
configured otherwise.

Create a small environment file readable only by your user, for example
`/home/your-user/.config/stock-monitor.env`:

```sh
OPENAI_API_KEY='your-key'
```

For a `venv` setup, add this alternative with `crontab -e` (runs daily at 07:15
local server time):

```cron
15 7 * * * cd {{ project_absolute_path }} && set -a && . /home/your-user/.config/stock-monitor.env && set +a && .venv/bin/python stock_news_monitor.py --config config.yml >> data/cron.log 2>&1
```

Use absolute paths and ensure the environment file has restrictive permissions
(`chmod 600`). The advice is based only on feed summaries, not live quotes or complete
company fundamentals, so it should be treated as a research aid rather than a trading
instruction.
