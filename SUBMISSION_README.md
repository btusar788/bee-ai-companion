# 🐝 Bee AI Dashboard - Smart Ambient Actions Companion

## 🚀 Project Overview
An intelligent, hands-free smartphone companion dashboard engineered specifically for the **Amazon Bee Wearable AI** ecosystem. Built using pure Python and the Flet framework, the application runs a highly resilient, non-blocking background ingestion thread that captures voice transcripts directly from local microphone arrays. 

Using custom regular expressions patterned after the `bee-skill` specification, it parses raw ambient audio streams in real-time, extracts text intent strings, and dynamically generates functional, interactive mobile checkbox task controls. During voice-flagged crisis triggers, the application instantly restructures its canvas viewport state to display an integrated emergency assistance center.

---

## ⚠️ Official Developer Friction Log (10% Bonus Point Track)
During development, the following platform bottlenecks, documentation ambiguities, and layout constraints were logged:

1. **Ecosystem & CLI Environment Path Mismatches:**
   - *Friction:* The hackathon setup frameworks globally refer to the developer package as `@beeai/cli`, yet on Windows environments, the global Node package installer (`npm`) shortcut utility command resolves exclusively to `bee`. Attempting to route `subprocess` terminal shell streams directly to `bee-cli` throws a terminal `FileNotFoundError`.
   - *Resolution:* Swapped execution strings to use the native `bee` hook, and elegantly decoupled code structure testing from cloud authentication locks by routing an independent hardware microphone capture handler (`SpeechRecognition` & `pyaudio`) straight into the thread-safe local network memory queue arrays.

2. **UI Container Viewport Text Overlapping:**
   - *Friction:* Initial horizontal layouts forced task labels, keyword hashtags (`#remind me to`, `#todo`), and precise system time strings onto a tight singular row element. On narrow mobile screen resolutions, this caused layout elements to violently overlap and truncate.
   - *Resolution:* Refactored the UI hierarchy tree, introducing an indented nested layout grid that drops hashtags and timestamps vertically underneath the main checkable task cards, providing complete readability.

3. **Background Voice Loop URL Flooding:**
   - *Friction:* Because the background polling thread checks data streams every fraction of a second, intercepting an external operation instruction (like "WhatsApp") caused `page.launch_url` to trigger recursively, flooding the computer desktop with a new browser tab every half-second.
   - *Resolution:* Implemented a strict "Execute Once" state tracking checkpoint flag variable right on the Flet `page` lifecycle context instance object, ensuring external deep links fire exactly once per spoken command.

---

## 🌟 Core Automated System Operations

*   **⚡ Ambient Task Extractor:** Listens to speech patterns like *"Todo finish the script"* or *"Remind me to buy coffee"*, automatically isolates the specific action phrase, attaches a custom structural tag, and spawns a checkbox item on screen.
*   **🚨 Dial Emergency Helpline:** Captures emergency voice flags (*"help me"*, *"accident"*, *"emergency"*) to show the safety center. Clicking this button triggers the universal telephony scheme (`tel:112`) to automatically pre-fill the dialer pad app on the user's phone, allowing them to rapidly call for a sudden ambulance or police squad.
*   **📍 Turn-by-Turn Route Navigation:** Intercepts location directions via voice commands or click controls and launches the system's primary native map app directly into turn-by-turn navigation mode, calculating quick routes to the closest hospital from their exact GPS coordinates.
*   **💬 Hands-Free WhatsApp Dispatch:** Encodes transcript text variables instantly into an API query payload, opening a native WhatsApp deep-link interface window pre-loaded with critical health profile updates ready to text saved emergency contacts.

---

## 🛠️ Tech Stack & Execution Parameters
- **Architecture Style:** 100% Pure Python (Antigravity Structural Paradigm)
- **UI Render Engine:** Flet Mobile UI Layout Framework
- **Audio Capture Pipelines:** SpeechRecognition, PyAudio Core Framework
- **Data & Concurrency Primitives:** Threading Workers, Reentrant State Locks (`threading.RLock`), Thread-Safe Queues
