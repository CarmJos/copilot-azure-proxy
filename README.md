# **Copilot Azure Proxy** _for JetBrains IDEs_

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-GPL%20v3-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

_**"Use any OpenAI-compatible model with JetBrains GitHub Copilot."**_

A lightweight, single-port proxy that translates Azure OpenAI API requests from JetBrains Copilot
into OpenAI-compatible backend calls — enabling you to use **DeepSeek**, **MIMO**,
or any custom model with full IDE integration.

## Features & Advantages

- **Single-Port Architecture** — One Python file, one port. No middleware chain, no external services.
- **Azure API Compatibility** — Full support for `/openai/deployments`, `/openai/models`, and chat completions
  endpoints.
- **Model Identity Emulation** — Use `base_model` to impersonate known Azure models and fix context-window display
  issues.
- **Multi-Model Support** — Configure multiple backends in `config.yaml` and switch via JetBrains' deployment selector.
- **Streaming & Non-Streaming** — Handles both SSE streaming and standard chat completions.
- **Graceful Error Handling** — Clean disconnection on client interrupt, Azure-format error responses.
- **Debug Mode** — Toggle `general.debug` to log full request bodies for troubleshooting.
- **Access Control** — Set `general.api-key` to require an API key on all proxy requests.
- **Zero Dependencies Beyond pip** — Only `aiohttp`, `litellm`, and `pyyaml`.

## Quick Start

### 1. Prerequisites

- Python **3.10+**
- Windows, macOS, or Linux

### 2. Clone & Setup

```shell
git clone https://github.com/CarmJos/copilot-azure-proxy
cd copilot-azure-proxy
```

Then run the init script to create .venv and install dependencies,

for Windows:
```shell
.init.bat
```

for macOS / Linux:
```shell
chmod +x .init.sh 
./.init.sh
```

> [!CAUTION]  
> If the init script fails, please open it with a text editor and
> inspect the commands. The script is straightforward — each step is a single shell
> command you can also run manually.

### 3. Configure Models

Edit `config.yaml` to define your backend models:

```yaml
general:
  port: 4000
  timeout: 120
  debug: false          # set true for request body logging
  api-key: ""           # optional — set to require an api-key header on all requests

models:
  - model_name: deepseek-v4-pro
    litellm_params:
      provider: openai
      model: openai/deepseek-v4-pro
      api_base: https://api.deepseek.com
      api_key: sk-YOUR-KEY
      max_tokens: 384000
      max_input_tokens: 1000000
      max_output_tokens: 384000
```

### 4. Run

**Windows:**

```shell
run.bat
```

**macOS / Linux:**

```shell
chmod +x run.sh
./run.sh
```

The proxy starts at `http://localhost:4000`.

### 5. Configure Your JetBrains IDE

1. Open the **Copilot Chat** panel.
2. Click the model selector dropdown in the chat input area.
3. Choose **Manage Models**.
4. Under the **Azure** provider section, click **+ Add models**.
5. Fill in the form for each model:

| Field              | Value                                                                  |
|--------------------|------------------------------------------------------------------------|
| **Model ID**       | Exact deployment name from `config.yaml` (e.g. `deepseek-v4-pro`)      |
| **Deployment URL** | `http://{host}:{port}/openai/deployments/{model-id}/chat/completions`  |
| **API key**        | Any value — unless `general.api-key` is set, then must match that key. |
| **Model name**     | The display name you like.                                             |
| **Toool**          | **Check** (otherwise "agent" mode is not supported)                    |
| **Vision**         | **Uncheck** (unless your backend supports image inputs)                |

> [!TIP]
> **Deployment URL** must contain the same model ID as the **Model ID** field.  
> Replace `{model-id}` with your actual deployment name, e.g.
`http://localhost:4000/openai/deployments/deepseek-v4-pro/chat/completions`.

After adding, the model will appear in your Copilot Chat model selector.

## Configuration Reference

### `config.yaml`

| Field                                      | Type | Description                                              |
|--------------------------------------------|------|----------------------------------------------------------|
| `general.port`                             | int  | Proxy listen port (default: `4000`)                      |
| `general.timeout`                          | int  | Per-request timeout in seconds (default: `120`)          |
| `general.debug`                            | bool | Log full POST request bodies when `true`                 |
| `general.api-key`                          | str  | Optional — require this `api-key` header on all requests |
| `models[].model_name`                      | str  | Deployment name shown in JetBrains                       |
| `models[].litellm_params.model`            | str  | LiteLLM model identifier (e.g. `openai/deepseek-v4-pro`) |
| `models[].litellm_params.api_base`         | str  | Backend API base URL                                     |
| `models[].litellm_params.api_key`          | str  | API key (or `os.environ/VAR` for env reference)          |
| `models[].litellm_params.base_model`       | str  | Optional — Azure model name to impersonate               |
| `models[].litellm_params.max_input_tokens` | int  | Reported context window size                             |

All `litellm_params` fields support Litellm's full parameter set (`temperature`, `supports_vision`,
`supports_function_calling`, `supports_reasoning`, `supports_tool_choice`, etc.).

### CLI Arguments

```
python copilot_azure_proxy.py --config config.yaml --port 4000 --host 0.0.0.0
```

| Flag       | Default       | Description              |
|------------|---------------|--------------------------|
| `--config` | `config.yaml` | Path to YAML config file |
| `--port`   | from config   | Override listen port     |
| `--host`   | `0.0.0.0`     | Bind address             |

## API Endpoints

| Method | Path                                          | Description                              |
|--------|-----------------------------------------------|------------------------------------------|
| `GET`  | `/openai/deployments`                         | List all configured deployments          |
| `GET`  | `/openai/deployments/{name}`                  | Single deployment detail                 |
| `GET`  | `/openai/deployments/{name}/models`           | Model info for a deployment              |
| `GET`  | `/openai/models`                              | Azure model catalog                      |
| `GET`  | `/v1/models`                                  | OpenAI-compatible model list             |
| `GET`  | `/v1/models/{name}`                           | Single model detail                      |
| `POST` | `/openai/deployments/{name}/chat/completions` | Chat completions (stream and non-stream) |
| `GET`  | `/health`                                     | Health check                             |
| `GET`  | `/logs`                                       | Recent log buffer (last 200 lines)       |


## Support and Donation

If you appreciate this plugin, consider supporting me with a donation at 
[GitHub Sponsors](https://github.com/sponsors/CarmJos) or
[爱发电](https://www.ifdian.net/a/carmjos/plan) !

**Thank you for supporting open-source projects!**

Many thanks to JetBrains for kindly providing a license for us to work on this and other open-source projects.

[![](https://resources.jetbrains.com/storage/products/company/brand/logos/jb_beam.svg)](https://www.jetbrains.com/?from=https://github.com/CarmJos/)

## Open Source License

This project's source code is licensed under
the [GNU General Public License, Version 3](https://www.gnu.org/licenses/gpl-3.0.html).
