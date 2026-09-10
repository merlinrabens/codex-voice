# Codex Voice

Built for Intel Mac users who cannot use the ChatGPT desktop app and want live voice with Codex. You run Codex CLI locally and talk through your browser. The desktop app is not required.

An experimental, unofficial local voice interface for Codex CLI. Talk while Codex works, send text to steer a task, and review supported approval requests in your browser.

Codex Voice starts your installed `codex app-server` and uses its experimental Realtime connection with your existing ChatGPT login. Task execution defaults to `gpt-6-astra`; OpenAI's voice channel is a separate model. This project does not bundle OpenAI software, credentials, a voice model, or a Computer Use runtime.

## Requirements

- Python 3.10 or later. The backend uses only the standard library.
- Codex CLI on `PATH`, signed in with `codex login` using your ChatGPT account.
- Account access to the requested Codex model and experimental Realtime voice. Availability and voice allowances depend on your account.
- A browser with WebRTC and microphone support. Chrome on macOS is the tested browser.

By default the interface and Codex run on the same computer. Optional paired HTTPS access lets a phone reach that computer while Codex continues to run there. A ChatGPT desktop installation is not required.

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

## Temporary iPhone access

With `cloudflared` installed, start a temporary HTTPS tunnel and the authenticated voice server:

```sh
python3 remote.py --cwd /path/to/your/project --keep-awake
```

Open the `pairing_url` from the private `~/.codex/voice-access/connection.json` file in Safari on your iPhone. Tap **Connect to my Mac**, then start voice and allow microphone access. The phone can use mobile data or a different Wi-Fi network. Add `--yolo` only when you want the same unrestricted task permissions as the local mode.

The link pairs one browser, expires after 30 minutes, and is consumed once. Its code is carried in the URL fragment, removed before the page makes requests, and exchanged for a Secure, HttpOnly, SameSite=Strict cookie after you tap the connect button. Treat the link as private. No pairing code or login token is stored in browser local storage.

The default login lasts up to 12 hours. Select **Remember this personal device for 30 days** before connecting to choose a 30-day login instead. This is a fixed expiry from pairing; activity does not extend it. Either choice works only while the same server and HTTPS origin remain available. Restarting the server revokes browser sessions, and restarting the temporary tunnel generates a different URL. The private receipt is kept outside this checkout. `POST /api/logout` revokes the current browser session.

If you return to the connection page without a code, your login may have expired or your browser may have cleared its cookie. An expired or consumed pairing link cannot sign you in again. From the project directory on the Mac, create a fresh link and copy it to the clipboard:

```sh
python3 remote.py --pair --copy
```

Open that new link on your phone and connect again. This command talks to the running launcher through a local control socket accessible only to your Mac user. It replaces the unused pairing code and updates the private receipt without restarting the server, tunnel, or active Codex conversation. A valid existing browser login returns to the voice interface automatically, including when an external link initially opens the connection page because of the strict cookie policy.

The Mac and launcher must stay running and online. `--keep-awake` prevents idle sleep on macOS while the launcher is running; it does not guarantee operation with a laptop lid closed. Keep Safari open during voice. If iOS interrupts the microphone or blocks playback, the page offers a restart or audio button. Real iPhone hardware behavior still needs device testing; browser emulation alone does not establish it.

Audio continues over WebRTC directly between the browser and OpenAI. Cloudflare proxies the authenticated interface and control requests, not an extra audio-processing pipeline. Network conditions still affect latency. Quick Tunnels are temporary development connections without an uptime guarantee and do not support SSE, so remote mode uses authenticated JSON long polling for task events. [Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)

Stop the launcher with **Ctrl+C** to stop the server and tunnel. No Cloudflare account or ChatGPT desktop app is required for this temporary mode.

## Optional conversation memory

Add `--memory-dir /path/to/private/vault/Conversations` to either launcher to keep finalized spoken turns, submitted text prompts and completed Astra text replies as private Markdown. One archive is maintained per Codex thread, with speaker, capture time and source IDs. Intermediate transcript fragments and tool outputs are not copied. The UI reports saved or failed writes. Files are written atomically and repeated source items are deduplicated; new final revisions preserve earlier text.

The archive is an unverified conversation source, not a list of established facts. Audio recordings and authentication tokens are not saved by this feature. Choose a folder outside the source checkout and covered by your own backup and retrieval setup. Archival is opt-in in the public project; no private folder is assumed.

If you already run a notes indexer, `--memory-index-script /path/to/indexer.py` triggers it after voice ends; `--memory-index-python /path/to/venv/bin/python3` selects its interpreter. This hook is configured locally, never by the phone. Index runs are serialized in the background. Writes arriving during a run request a follow-up, and startup failures or nonzero exits retry with backoff. The indexer must return a nonzero status when it fails or cannot acquire its lock. Successful file synchronization does not establish completed embeddings.

Two conversations started in quick succession keep separate archives when they use different Codex threads. Resuming the same thread appends to its existing archive. Late finalized events from a known earlier thread are saved to that original thread and request indexing without changing the active conversation. The existing indexer can discover archives during longer conversations as well. Search freshness depends on its schedule and embedding completion; saved Markdown is available for direct file-based search. This feature does not automatically inject earlier conversations into a new thread, extract accepted decisions, update a wiki, or capture unrelated Codex CLI sessions.

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

The HTTP server binds to `127.0.0.1` and checks host and request-origin headers. Default local mode has no account system; other software running on your computer can reach the loopback service. Use `remote.py` for internet access: it explicitly enables session authentication on every private route, including loopback requests. Do not publish an unauthenticated local-mode server through a proxy.

Microphone audio is sent to OpenAI through the browser's WebRTC connection. Project context and task activity are handled by your Codex session. The application does not save audio recordings, copy authentication files, or log HTTP request bodies. Codex retains its normal session history according to your configuration. Existing ChatGPT plan access, usage limits, and voice allowances still apply.

## Verification and limits

Tested with Codex CLI `0.153.4` on Intel macOS and Chrome. A synthetic audio signal was sent over the real WebRTC connection, audio output was received, and a spoken task was handed to an Astra turn that completed. That test did not establish microphone-hardware quality, speaker playback, or reliable interruption by speaking over a response.

The Realtime app-server protocol is experimental and may change. A successful connection on one account does not establish availability for every account. See [OpenAI's voice documentation](https://learn.chatgpt.com/docs/features/voice).

Offline checks do not call a model, use a microphone, or operate the desktop:

```sh
python3 tests.py
python3 tests_remote.py
python3 tests_memory.py
python3 tests_memory_integration.py
node tests_pair_ui.js
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
