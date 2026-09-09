# Codex Voice

Built for Intel Mac users who cannot use the ChatGPT desktop app and want live voice with Codex. You run Codex CLI locally and talk through your browser. The desktop app is not required.

An experimental, unofficial local voice interface for Codex CLI. Talk while Codex works, send text to steer a task, and review supported approval requests in your browser.

Codex Voice starts your installed `codex app-server` and uses its experimental Realtime connection with your existing ChatGPT login. Task execution defaults to `gpt-6-astra`; OpenAI's voice channel is a separate model. This project does not bundle OpenAI software, credentials, a voice model, or a Computer Use runtime.

## Requirements

- Python 3.10 or later. The backend uses only the standard library.
- Codex CLI on `PATH`, signed in with `codex login` using your ChatGPT account.
- Account access to the requested Codex model and experimental Realtime voice. Availability and voice allowances depend on your account.
- A browser with WebRTC and microphone support. Chrome on macOS is the tested browser.

The interface and Codex run on the same computer. This is a local application, not a public web service or a hosted Codex session.

## Run

Clone or download this repository, then run from its directory:

```sh
python3 server.py --cwd /path/to/your/project
```

Open [http://127.0.0.1:8766](http://127.0.0.1:8766), choose your project folder and reasoning effort, and start a conversation. Microphone access is requested when you start voice, not when the page loads.

Use `--port 8767` to choose another port or `--model MODEL_ID` to select a different task model. Without `--cwd`, the working directory is where you launched the command.

### Optional command installation

Keep this source directory in place. From the repository root:

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/bin/codex-voice" "$HOME/.local/bin/codex-voice"
```

Add `$HOME/.local/bin` to your shell's `PATH` if needed. You can then run `codex-voice` from any project directory. The launcher resolves its symlink and finds `server.py` and `static/` in this checkout. There is no package installation or background service.

```sh
codex-voice --cwd /path/to/your/project --port 8766
```

If that command name already exists, use `./bin/codex-voice` directly or choose another symlink name.

## How the session works

- Voice streams continuously over WebRTC after you connect. You do not need to submit each spoken message.
- Text starts a task or steers the active task. The interrupt control stops active Codex work.
- Muting pauses microphone input. Ending the conversation closes voice. Stop the local server with **Ctrl+C** when finished.
- The microphone indicator follows the local input signal's RMS level. It is a visual meter, adds no voice processing, and does not indicate whether speech recognition succeeded. Reduced-motion preferences disable its animation.
- The page creates its own Codex session using the selected project's instructions and your Codex configuration and skills. It does not attach to an already-running terminal conversation.
- The displayed session ID can later be opened with `codex resume SESSION_ID`.
- Some interactive Codex tools require the full Codex client. Unsupported approval or interaction types are not automatically accepted.

## Permissions

Ask mode is the default: Codex uses its workspace sandbox and requests approval when required. The interface presents the decisions supported by each native approval request. Session approvals apply to that session; an always-allow command prefix is available only when Codex provides a matching rule amendment. Read the proposed command, scope, and file changes before approving.

Use the access selector to choose the mode for a new session. Explicit choices are saved in this browser. Without a saved or newly selected choice, the page follows the server's default. The YOLO badge describes the active session, so changing the selector does not change a task already in progress.

You can deliberately start with `--yolo` to make Codex's `never` approval policy and `danger-full-access` sandbox setting the server default. This permits local commands and file changes without an approval prompt. An explicit saved browser choice takes precedence for the next session.

The session instructs Codex to use native OpenAI tools for desktop control. This is an instruction, not a security boundary: unrestricted shell access in YOLO mode cannot guarantee that only one automation runtime will be used.

## Computer Use

This interface can work with capabilities available to its Codex session. It does not install or replace native OpenAI Computer Use. Follow [OpenAI's Computer Use setup](https://learn.chatgpt.com/docs/computer-use) for that runtime and its operating-system permissions.

In the official Intel macOS desktop build inspected on September 9, 2026 (`26.903.61454`, build `8378`), the native Computer Use service was absent. The included JavaScript plugin alone did not provide a working native desktop-control setup. This is a finding about that build, not a claim that Intel support is permanently unavailable. No third-party desktop-control replacement is included.

## Data and access

The HTTP server binds to `127.0.0.1` and checks local host and request-origin headers. Keep it local; it has no account system or remote-access authentication. Other software running on your computer can reach a loopback service.

Microphone audio is sent to OpenAI through the browser's WebRTC connection. Project context and task activity are handled by your Codex session. The application does not save audio recordings, copy authentication files, or log HTTP request bodies. Codex retains its normal session history according to your configuration. Existing ChatGPT plan access, usage limits, and voice allowances still apply.

## Verification and limits

Tested with Codex CLI `0.153.4` on Intel macOS and Chrome. A synthetic audio signal was sent over the real WebRTC connection, audio output was received, and a spoken task was handed to an Astra turn that completed. That test did not establish microphone-hardware quality, speaker playback, or reliable interruption by speaking over a response.

The Realtime app-server protocol is experimental and may change. A successful connection on one account does not establish availability for every account. See [OpenAI's voice documentation](https://learn.chatgpt.com/docs/features/voice).

Offline checks do not call a model, use a microphone, or operate the desktop:

```sh
python3 tests.py
python3 scripts/publication_audit.py
```

The optional browser test uses Playwright in a separate Python environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install playwright
python3 tests_ui.py
```

It uses an installed macOS Chrome when available. Otherwise install Playwright's Chromium with `python3 -m playwright install chromium` before running the test. The test creates its own temporary server and browser, uses synthetic audio for the level meter, and does not connect to Codex or use a real microphone. Playwright is only a test dependency.

The publication audit is a heuristic source check, not a guarantee that a repository contains no sensitive information. It prints filenames and rule names without printing matched values. Use repeatable `--private-term` arguments to check additional organization-specific terms locally.

## License

[MIT](LICENSE). This is an independent project and is not affiliated with or endorsed by OpenAI.
