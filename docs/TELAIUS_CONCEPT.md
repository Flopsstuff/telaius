# Telaius — High-Level Concept

**Project:** [Fl0p/telaius](https://github.com/Fl0p/telaius)  
**Name:** *Telephone + Aius Locutius*, the Roman disembodied voice.  
**Status:** Concept / MVP design brief.

## 1. Vision

Telaius is a self-hosted **outbound telephone calling tool for AI agents**. An external agent delegates a real-world communication task, and Telaius calls a regular telephone number, carries out a natural spoken conversation, then reports the result in a structured form.

The first practical use case is calling private car sellers to gather information, but **the platform must be domain-agnostic**: calling stores, services, venues, individuals, etc.

This is **not** a mass-dialing or marketing platform, nor a general-purpose autonomous assistant. It is a focused *telephone execution capability* for other agents.

## 2. Core Product Decision

**Cellular telephony is local hardware, not a cloud telephony API.**

- Host: Raspberry Pi (initially Pi 5; avoid unnecessary host assumptions).
- Cellular modem: **SIMCom SIM7600G-H**, connected over **USB**, with a regular registered Polish SIM card.
- Calls: standard outbound PSTN/mobile calls via the cellular operator (prefer VoLTE where supported).
- Voice conversation: **xAI Realtime Voice API** for live speech-to-speech processing.
- Agent integration: **MCP first**, optional REST API later.
- No Twilio, Telnyx, SIP trunk, inbound number procurement, physical microphone, speaker, or GNSS needed for the MVP.

**Important:** USB modem control and USB bidirectional call audio are distinct capabilities. A successful AT dialing command does *not* prove that real-time audio is available. Modem firmware, USB interface configuration, carrier/IMS/VoLTE compatibility and power supply all matter.

## 3. Conceptual Flow

```text
External AI agent
  |
  | MCP: make_call(phone, goal, context, constraints, result_schema)
  v
Telaius orchestration service (Raspberry Pi)
  |  task validation / call lifecycle / safety limits / results
  |
  +---- Modem adapter ---- USB AT commands ------+
  |                                               |
  +---- Audio bridge <--- USB digital PCM ------> SIM7600G-H + SIM
  |         |                                     |
  |         +<----> xAI Realtime Voice API        +--> Cellular network
  |                                                    |
  +---- Transcript, outcome, structured report <--- Remote callee
  |
  v
External AI agent receives result
```

During a call, the audio bridge handles **both directions**: caller speech -> modem USB audio -> xAI, and xAI generated speech -> USB audio -> callee. A call is outbound, but its **audio is full-duplex**.

## 4. Functional Contract

An external agent supplies:

- Destination phone number (prefer E.164 format).
- Purpose of the call and relevant context.
- Questions / desired information or outcome.
- Guardrails (what Telaius must not promise, agree to, disclose, or do).
- Optional desired language and result schema.

Telaius then:

1. Validates permissions, call limits and hardware readiness.
2. Starts one outgoing telephone call through the SIM7600G-H.
3. Establishes and supervises a real-time xAI conversation.
4. Introduces itself transparently as an AI assistant calling on the user's behalf.
5. Follows the delegated objectives, asks clarifying questions and handles interruptions.
6. Recognizes refusal, voicemail, no answer, busy, failure and hangup.
7. Terminates cleanly, closes streaming resources and produces an outcome report.

The caller must not need to know about AT commands, USB devices, PCM buffering or xAI audio frames.

## 5. Interface (MCP-first)

Candidate MCP tools:

- `make_call({phone_number, task, context?, constraints?, language?, result_schema?})` -> `{call_id, status}`
- `get_call_status({call_id})` -> lifecycle, timestamps, error if any
- `get_call_result({call_id})` -> summary, answers, transcript if available, structured data, unresolved points
- `end_call({call_id})` -> cancel / hang up

Calls are **long-running asynchronous jobs**. `make_call` should return quickly. The initial version supports **one active call per modem**. Do not initiate calls without explicit per-call authorization from the requesting user/agent workflow.

## 6. Components

### Modem adapter

Discovers USB interfaces, opens AT control channel, checks SIM/network, dials, monitors call events, hangs up, and reports failures. Device paths such as `/dev/ttyUSB2` (AT) and `/dev/ttyUSB4` (PCM) are **examples, not guarantees**. Discover/configure them at runtime. Support modem reset/recovery.

### Digital audio bridge

Bi-directional PCM transport between the modem and xAI; audio-format conversion and resampling as needed; synchronized reads/writes; pacing, small bounded buffers, backpressure, interrupt/barge-in handling and stall detection. **No ALSA device should be assumed**: on documented SIM7600 configurations, audio is available as a binary stream via a USB serial interface rather than USB Audio Class.

### Voice session

Manages xAI Realtime connection, conversation instructions, audio input/output, tool events, end-of-call behavior and session lifecycle. Polish dialogue quality, end-to-end latency and actual supported audio formats need live tests. Keep the interface modular so the voice engine can be replaced later.

### Call orchestrator and results

Maintains lifecycle states, ties together modem and voice session, applies timeouts/cost limits, persists metadata, and converts observed conversation into an attributable summary and structured result. Uncertain answers must remain marked as uncertain rather than fabricated.

## 7. Hardware Reference and Critical Unknowns

**Primary implementation reference:**

- [The-OASIS-Project/dawn](https://github.com/The-OASIS-Project/dawn)
- [DAWN — Phone/SMS Design](https://github.com/The-OASIS-Project/dawn/blob/main/docs/PHONE_SMS_DESIGN.md)

That project documents successful **bidirectional SIM7600G-H USB PCM audio** on a particular hardware/firmware combination, including the need to synchronize audio I/O. It is a **reference, not proof** that every SIM7600G-H board or firmware behaves identically.

Additional reference: [SIM7600_VOLTEcall](https://github.com/aaronshi2017/SIM7600_VOLTEcall) (outbound calling / PCM playback example).

**Validate on the actual purchased unit:**

- Exact board/form factor and power arrangement.
- Firmware (`AT+CGMR`), available AT commands and USB endpoints.
- SIM registration, mobile network and ability to place voice calls in Poland.
- VoLTE/IMS compatibility with the chosen operator, or a viable fallback.
- USB PCM capture *and* playback during the same real call.
- Sample rate, framing, latency and stream stability.

Do not make firmware flashing or modifications an automatic first step; inspect the factory setup and preserve a recovery path.

## 8. MVP Milestones and Exit Criteria

**M0 — Hardware proof of concept**

- Detect the modem on Raspberry Pi via USB.
- Query firmware, SIM and radio status.
- Dial and terminate a test call.
- Capture incoming conversation audio to a file.
- Inject a WAV/PCM test phrase into the remote call.
- Prove **simultaneous two-way USB audio**, with neither microphone nor speakers attached.

*Exit criterion:* a reproducible CLI test call with working audio in both directions. **This is the gating milestone.**

**M1 — Live voice conversation**

- Connect modem PCM to xAI Realtime.
- Establish a short natural telephone dialogue.
- Test Polish and turn-taking; measure latency and dropouts.
- Handle hangup/timeout/reconnection without orphaned calls.

*Exit criterion:* a real person can converse intelligibly with the AI by phone.

**M2 — Agent integration**

- Expose MCP tools and asynchronous call state.
- Pass dynamic conversation goals and boundaries.
- Return structured answers, concise summary and failure status.
- Add configuration, logs and repeatable deployment on Raspberry Pi.

*Exit criterion:* an external agent can delegate a call and receive a useful report without touching telephony internals.

## 9. Guardrails

- Disclose the AI identity; never impersonate the user's own live voice/person without clear disclosure.
- Respect refusal, opt-outs and do-not-contact requests.
- No unsolicited bulk calls; no unapproved purchases, bookings, payments or binding commitments.
- Per-call authorization, destination restrictions, rate/concurrency limits and maximum duration.
- Confirm applicable laws and carrier terms for automated calling and for recording/transcribing calls; minimize retention of personal data.
- Keep xAI credentials and phone logs protected, and avoid leaking private context to callees.

## 10. Deliberately Out of Scope

Inbound answering, call centers/campaigns, multi-modem pools, web UI, CRM integrations, GPS/GNSS, a separate cloud telephony provider, and sophisticated scheduling/negotiation workflows are **not MVP requirements**.

## 11. Implementation Guidance to the Building Agent

**Do not start by designing a large system.** First inspect the existing [Telaius repository](https://github.com/Fl0p/telaius), then study DAWN's audio implementation. Document what is known versus hardware-dependent. Produce a minimal reproducible USB call-and-audio test for the actual SIM7600G-H before wiring in xAI or designing an extensive MCP layer.

Treat the architecture above as intent and boundaries, not mandatory module names or an inflexible technology prescription. Favor the smallest reliable implementation, and explicitly report unresolved compatibility risks.
