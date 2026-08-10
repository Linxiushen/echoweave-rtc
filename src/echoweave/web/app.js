const EW = {
  ws: null,
  audioContext: null,
  micStream: null,
  micProcessor: null,
  micSource: null,
  micSilentGain: null,
  micWorkletLoaded: null,
  micDroppedFrames: 0,
  microphoneRequested: false,
  micRequestEpoch: 0,
  micPending: null,
  playbackSources: new Set(),
  scheduledAt: 0,
  ttsSampleRate: 48000,
  ttsChannels: 1,
  ttsFormatValid: false,
  audioProtocolErrors: 0,
  audioDroppedFrames: 0,
  currentAssistant: null,
  currentAssistantParts: [],
  assistantRenderFrame: null,
  started: false,
  startPending: false,
  videoQueue: [],
  playingVideo: false,
  currentVideoUrl: null,
  syncAudioTurns: new Map(),
  playoutEpoch: 0,
  helloReceived: false,
  startSent: false,
  startAttempt: 0,
  textPending: false,
  personaName: "Echo",
  negotiatedCapabilities: [],
  lastSequence: 0,
  pingTimer: null,
  heartbeatSocket: null,
  lastPongAt: 0,
  lastRttMs: null,
  fatalError: false,
  retryableError: false,
  sessionState: "offline",
};
const MAX_VIDEO_QUEUE = 4;
const MAX_VIDEO_BYTES = 32 * 1024 * 1024;
const MAX_TTS_FRAME_BYTES = 512 * 1024;
const MAX_SYNC_AUDIO_BYTES = 4 * 1024 * 1024;
const MAX_AUDIO_BACKLOG_SECONDS = 2.0;
const MAX_WS_BUFFERED_BYTES = 256 * 1024;
const HEARTBEAT_INTERVAL_MS = 10_000;
const HEARTBEAT_TIMEOUT_MS = 30_000;
const MAX_SESSION_TOKEN_CHARS = 4096;
const MAX_TRANSCRIPT_ITEMS = 80;
const transcriptFrame = window.requestAnimationFrame?.bind(window)
  || ((callback) => window.setTimeout(callback, 16));
const cancelTranscriptFrame = window.cancelAnimationFrame?.bind(window)
  || window.clearTimeout.bind(window);
const reducedMotionPreference = window.matchMedia?.("(prefers-reduced-motion: reduce)") || null;

const $ = (selector) => document.querySelector(selector);
const startButton = $("#startButton");
const cancelButton = $("#cancelButton");
const endButton = $("#endButton");
const sendButton = $("#sendButton");
const textInput = $("#textInput");
const transcript = $("#transcript");
const disclosureAck = $("#disclosureAck");
const personaInput = $("#personaInput");
const accessTokenInput = $("#accessTokenInput");
const avatar = $("#syntheticAvatar");
const avatarVideo = $("#avatarVideo");
const stateLabel = $("#stateLabel");
const connectionDot = $("#connectionDot");
const toast = $("#toast");
const networkQuality = $("#networkQuality");
const networkRtt = $("#networkRtt");
const turnLatency = $("#turnLatency");
const firstTokenLatency = $("#firstTokenLatency");
const pipelineStages = $("#pipelineStages");
const capabilityList = $("#capabilityList");
const sessionSequence = $("#sessionSequence");
const recoveryAction = $("#recoveryAction");
const activityLog = $("#activityLog");
const transcriptAnnouncements = $("#transcriptAnnouncements");
const transcriptSessionState = $("#transcriptSessionState");
const transcriptStatusIndicator = $("#transcriptStatusIndicator");
const operationsLiveState = $("#operationsLiveState");
const operationsLiveLabel = $("#operationsLiveLabel");
const transportSecurityLabel = $("#transportSecurityLabel");
const transportSecurityText = $("#transportSecurityText");
const fragmentParams = new URLSearchParams(location.hash.slice(1));
accessTokenInput.value = fragmentParams.get("token") || "";
personaInput.value = fragmentParams.get("persona") || "demo";
if (location.hash) {
  history.replaceState(null, "", `${location.pathname}${location.search}`);
}

const pageHostname = location.hostname || location.host.split(":")[0];
const loopbackPage = ["127.0.0.1", "localhost", "::1"].includes(pageHostname);
const encryptedPage = location.protocol === "https:";
if (transportSecurityLabel) {
  transportSecurityLabel.dataset.security = encryptedPage
    ? "encrypted"
    : loopbackPage
      ? "local"
      : "insecure";
  setText(transportSecurityText, encryptedPage
    ? " ENCRYPTED MEDIA PATH"
    : loopbackPage
      ? " LOCAL MEDIA PATH"
      : " INSECURE MEDIA PATH");
}

const CLIENT_CAPABILITIES = [
  "control.barge_in",
  "control.cancel",
  "control.ping",
  "input.audio_pcm16",
  "input.text",
  "output.audio_pcm16",
  "output.avatar_events",
  "output.browser_tts",
  "output.text_stream",
  "output.video_fragments",
];
const STAGE_ORDER = ["vad", "asr", "llm", "tts", "avatar"];
const STAGE_BACKEND_LABELS = {
  vad: {
    energy: "Energy VAD",
    silero_v5: "Silero v5",
  },
  asr: {
    demo: "Demo ASR",
    qwen_local: "Qwen3 local",
    qwen_http: "Qwen3 worker",
  },
  llm: {
    demo: "Demo LLM",
    deepseek: "DeepSeek",
    openai_compatible: "Hosted API",
  },
  tts: {
    browser: "Browser TTS",
    voxcpm_local: "VoxCPM2 local",
    voxcpm_http: "VoxCPM2 worker",
  },
  avatar: {
    client_lipsync: "Client lipsync",
    soulx_http: "SoulX worker",
  },
};
const CAPABILITY_LABELS = {
  "control.barge_in": "语音打断",
  "control.cancel": "可打断",
  "control.ping": "链路心跳",
  "input.audio_pcm16": "实时语音",
  "input.text": "文字输入",
  "output.audio_pcm16": "流式声音",
  "output.avatar_events": "表情事件",
  "output.browser_tts": "浏览器语音回退",
  "output.text_stream": "流式文本",
  "output.video_fragments": "流式数字人",
};

function setText(element, value) {
  if (element) element.textContent = String(value);
}

function announceTranscript(message) {
  if (transcriptAnnouncements) transcriptAnnouncements.textContent = message;
}

function isTranscriptNearEnd() {
  const remaining = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;
  return !Number.isFinite(remaining) || remaining <= 72;
}

function followTranscript(shouldFollow) {
  if (shouldFollow) transcript.scrollTop = transcript.scrollHeight;
}

function trimTranscriptHistory() {
  while (transcript.children.length > MAX_TRANSCRIPT_ITEMS) {
    transcript.firstElementChild?.remove();
  }
}

function flushAssistantDelta() {
  EW.assistantRenderFrame = null;
  const paragraph = EW.currentAssistant?.querySelector("p");
  if (!paragraph) return;
  const shouldFollow = isTranscriptNearEnd();
  paragraph.textContent = EW.currentAssistantParts.join("");
  followTranscript(shouldFollow);
}

function appendAssistantDelta(text) {
  if (!EW.currentAssistant) {
    EW.currentAssistantParts = [];
    EW.currentAssistant = addMessage("assistant", "", true);
  }
  EW.currentAssistantParts.push(text);
  if (EW.assistantRenderFrame === null) {
    EW.assistantRenderFrame = transcriptFrame(flushAssistantDelta);
  }
}

function finishAssistantStream(finalText = null) {
  if (EW.assistantRenderFrame !== null) {
    cancelTranscriptFrame(EW.assistantRenderFrame);
    EW.assistantRenderFrame = null;
  }
  const paragraph = EW.currentAssistant?.querySelector("p");
  const shouldFollow = isTranscriptNearEnd();
  if (paragraph) {
    paragraph.textContent = finalText === null ? EW.currentAssistantParts.join("") : finalText;
  }
  EW.currentAssistant?.classList.remove("streaming");
  EW.currentAssistant = null;
  EW.currentAssistantParts = [];
  followTranscript(shouldFollow);
}

function addActivity(message, tone = "neutral") {
  if (!activityLog) return;
  const item = document.createElement("li");
  item.dataset.tone = tone;
  const time = document.createElement("time");
  time.dateTime = new Date().toISOString();
  time.textContent = new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const text = document.createElement("span");
  text.textContent = message;
  item.append(time, text);
  activityLog.prepend(item);
  while (activityLog.children.length > 8) activityLog.lastElementChild.remove();
}

function setRecovery(message = "", visible = false) {
  if (!recoveryAction) return;
  recoveryAction.hidden = false;
  recoveryAction.dataset.state = visible ? "actionable" : "idle";
  const label = recoveryAction.querySelector("[data-recovery-label], strong");
  const detail = recoveryAction.querySelector("[data-recovery-detail], small");
  if (label) label.textContent = message || "链路运行正常";
  if (detail) {
    detail.textContent = visible
      ? "不会重复发送上一条内容"
      : "异常时可在此安全重试";
  }
  if ("disabled" in recoveryAction) recoveryAction.disabled = !visible;
}

function renderNetwork(rttMs = null) {
  let quality = "offline";
  let label = "离线";
  if (EW.ws?.readyState === WebSocket.OPEN) {
    if (!Number.isFinite(rttMs)) {
      quality = "connecting";
      label = "链路检测中";
    } else if (rttMs < 80) {
      quality = "excellent";
      label = "极佳";
    } else if (rttMs < 180) {
      quality = "good";
      label = "稳定";
    } else if (rttMs < 350) {
      quality = "fair";
      label = "一般";
    } else {
      quality = "poor";
      label = "拥塞";
    }
  }
  if (networkQuality) {
    networkQuality.dataset.quality = quality;
    networkQuality.textContent = label;
  }
  setText(networkRtt, Number.isFinite(rttMs) ? `${Math.round(rttMs)} ms` : "— ms");
}

function renderCapabilities(capabilities = []) {
  EW.negotiatedCapabilities = [...capabilities];
  if (!capabilityList) return;
  capabilityList.replaceChildren();
  for (const capability of capabilities) {
    const chip = document.createElement("span");
    chip.className = "capability-chip";
    chip.title = capability;
    chip.textContent = CAPABILITY_LABELS[capability] || capability;
    capabilityList.appendChild(chip);
  }
  if (!capabilities.length) {
    const empty = document.createElement("span");
    empty.className = "capability-chip muted";
    empty.textContent = "等待协商";
    capabilityList.appendChild(empty);
  }
}

function renderRuntimeBackends(backends = {}) {
  if (!pipelineStages) return;
  for (const node of pipelineStages.querySelectorAll("[data-stage]")) {
    const stage = node.dataset.stage;
    const backend = typeof backends[stage] === "string" ? backends[stage] : "";
    const label = STAGE_BACKEND_LABELS[stage]?.[backend] || "未报告";
    setText(node.querySelector("small"), label);
    if (backend) node.title = `${stage.toUpperCase()}: ${backend}`;
    else node.removeAttribute("title");
  }
}

function renderPipeline(activeStage = null, completedThrough = null, degradedStage = null) {
  if (!pipelineStages) return;
  const activeIndex = STAGE_ORDER.indexOf(activeStage);
  const completedIndex = STAGE_ORDER.indexOf(completedThrough);
  for (const node of pipelineStages.querySelectorAll("[data-stage]")) {
    const index = STAGE_ORDER.indexOf(node.dataset.stage);
    const status = node.dataset.stage === degradedStage
      ? "degraded"
      : node.dataset.stage === activeStage
        ? "active"
        : index >= 0 && index <= Math.max(completedIndex, activeIndex - 1)
          ? "complete"
          : "idle";
    node.dataset.status = status;
    node.classList.toggle("active", status === "active");
    node.classList.toggle("complete", status === "complete");
    node.classList.toggle("degraded", status === "degraded");
    if (status === "active") node.setAttribute("aria-current", "step");
    else node.removeAttribute("aria-current");
  }
}

function trackSequence(event) {
  if (!Number.isInteger(event.sequence)) return;
  if (event.type === "session.hello") EW.lastSequence = 0;
  if (EW.lastSequence && event.sequence !== EW.lastSequence + 1) {
    addActivity(`事件序列从 ${EW.lastSequence} 跳到 ${event.sequence}`, "warning");
  }
  EW.lastSequence = event.sequence;
  setText(sessionSequence, `#${event.sequence}`);
}

function failHeartbeat(socket, activityMessage) {
  if (EW.heartbeatSocket !== socket || EW.ws !== socket) return;
  stopHeartbeat(socket);
  EW.started = false;
  EW.microphoneRequested = false;
  stopMicrophone();
  endButton.disabled = true;
  connectionDot.classList.remove("online");
  setState("closed");
  if (networkQuality) {
    networkQuality.dataset.quality = "poor";
    networkQuality.textContent = "心跳超时";
  }
  setRecovery("重新建立安全会话", true);
  addActivity(activityMessage, "error");
  try { socket.close(4000, "heartbeat_timeout"); } catch (_) { /* already closed */ }
}

function startHeartbeat(socket) {
  stopHeartbeat();
  EW.heartbeatSocket = socket;
  EW.lastPongAt = Date.now();
  const heartbeat = () => {
    if (EW.heartbeatSocket !== socket || EW.ws !== socket) return;
    if (socket.readyState !== WebSocket.OPEN) return;
    const now = Date.now();
    if (now - EW.lastPongAt >= HEARTBEAT_TIMEOUT_MS) {
      failHeartbeat(socket, "网关心跳超时，已停止麦克风并隔离旧连接");
      return;
    }
    try {
      socket.send(JSON.stringify({ type: "ping", client_time_ms: now }));
    } catch (_) {
      failHeartbeat(socket, "心跳发送失败，已停止麦克风并隔离旧连接");
    }
  };
  heartbeat();
  EW.pingTimer = window.setInterval(heartbeat, HEARTBEAT_INTERVAL_MS);
}

function stopHeartbeat(socket = null) {
  if (socket && EW.heartbeatSocket && EW.heartbeatSocket !== socket) return;
  window.clearInterval(EW.pingTimer);
  EW.pingTimer = null;
  EW.heartbeatSocket = null;
  EW.lastPongAt = 0;
  EW.lastRttMs = null;
  renderNetwork();
}

renderCapabilities();
renderPipeline();
renderNetwork();
setRecovery();
cancelButton.disabled = true;
endButton.disabled = true;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 4200);
}

function addSystemNote(text) {
  const shouldFollow = isTranscriptNearEnd();
  const item = document.createElement("div");
  item.className = "system-note";
  item.innerHTML = `<span>系统</span><p></p>`;
  item.querySelector("p").textContent = text;
  transcript.appendChild(item);
  trimTranscriptHistory();
  followTranscript(shouldFollow);
}

function addMessage(role, text = "", streaming = false) {
  const shouldFollow = isTranscriptNearEnd();
  const item = document.createElement("div");
  item.className = `message ${role}${streaming ? " streaming" : ""}`;
  item.innerHTML = `<span class="role"></span><p></p>`;
  item.querySelector(".role").textContent =
    role === "user" ? "YOU" : `${EW.personaName.toUpperCase()} · AI`;
  item.querySelector("p").textContent = text;
  transcript.appendChild(item);
  trimTranscriptHistory();
  followTranscript(shouldFollow);
  return item;
}

function setState(state) {
  const labels = {
    ready: "准备就绪",
    listening: "正在聆听",
    user_speaking: "检测到语音",
    transcribing: "正在识别",
    thinking: "正在思考",
    speaking: "正在回答",
    closed: "已关闭",
  };
  EW.sessionState = state;
  stateLabel.textContent = labels[state] || state;
  const sessionActive = state !== "closed";
  setText(transcriptSessionState, sessionActive ? "会话中" : "已离线");
  if (transcriptStatusIndicator) {
    transcriptStatusIndicator.dataset.status = sessionActive ? "live" : "offline";
  }
  if (operationsLiveState) {
    operationsLiveState.dataset.status = sessionActive ? "live" : "offline";
    setText(operationsLiveLabel, sessionActive ? "LIVE" : "OFFLINE");
  }
  cancelButton.disabled = !["transcribing", "thinking", "speaking"].includes(state);
  avatar.classList.toggle("listening", state === "listening" || state === "user_speaking");
  avatar.classList.toggle("speaking", state === "speaking");
  if (state === "ready" || state === "listening") renderPipeline();
  if (state === "user_speaking") renderPipeline("vad");
  if (state === "transcribing") renderPipeline("asr", "vad");
  if (state === "thinking") renderPipeline("llm", "asr");
  if (state === "speaking") renderPipeline("tts", "llm");
  if (state === "closed") {
    connectionDot.classList.remove("online");
    renderPipeline();
  }
}

function sendControl(payload) {
  if (EW.ws?.readyState === WebSocket.OPEN) {
    EW.ws.send(JSON.stringify(payload));
    return true;
  }
  return false;
}

function mediaPacket(kind, turnId, ptsMs, payload) {
  const body = payload instanceof Uint8Array ? payload : new Uint8Array(payload);
  const packet = new Uint8Array(12 + body.byteLength);
  packet[0] = 0x45;
  packet[1] = 0x57;
  packet[2] = 1;
  packet[3] = kind;
  const view = new DataView(packet.buffer);
  view.setUint32(4, turnId >>> 0, true);
  view.setUint32(8, ptsMs >>> 0, true);
  packet.set(body, 12);
  return packet.buffer;
}

function parsePacket(buffer) {
  const view = new DataView(buffer);
  if (view.byteLength < 12 || view.getUint8(0) !== 0x45 || view.getUint8(1) !== 0x57) {
    throw new Error("Invalid EchoWeave media packet");
  }
  if (view.getUint8(2) !== 1) {
    throw new Error("Unsupported EchoWeave media protocol version");
  }
  return {
    version: view.getUint8(2),
    kind: view.getUint8(3),
    turnId: view.getUint32(4, true),
    ptsMs: view.getUint32(8, true),
    payload: buffer.slice(12),
  };
}

function resample(input, inputRate, outputRate) {
  if (inputRate === outputRate) return input;
  const ratio = inputRate / outputRate;
  const length = Math.max(1, Math.round(input.length / ratio));
  const output = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const position = i * ratio;
    const left = Math.floor(position);
    const right = Math.min(input.length - 1, left + 1);
    const mix = position - left;
    output[i] = input[left] * (1 - mix) + input[right] * mix;
  }
  return output;
}

function floatToPCM16(floatSamples) {
  const output = new Int16Array(floatSamples.length);
  for (let i = 0; i < floatSamples.length; i += 1) {
    const value = Math.max(-1, Math.min(1, floatSamples[i]));
    output[i] = value < 0 ? value * 32768 : value * 32767;
  }
  return new Uint8Array(output.buffer);
}

function sendMicrophonePCM(pcm) {
  const ws = EW.ws;
  if (ws?.readyState !== WebSocket.OPEN || !EW.started) return;
  if (ws.bufferedAmount > MAX_WS_BUFFERED_BYTES) {
    EW.micDroppedFrames += 1;
    if (EW.micDroppedFrames === 1 || EW.micDroppedFrames % 100 === 0) {
      showToast("网络上行拥塞：已丢弃过期麦克风帧，避免对话延迟继续累积。");
    }
    return;
  }
  const pts = Math.floor(performance.now()) >>> 0;
  ws.send(mediaPacket(1, 0, pts, pcm));
}

async function createMicrophoneProcessor(context) {
  if (context.audioWorklet && typeof AudioWorkletNode !== "undefined") {
    try {
      if (!EW.micWorkletLoaded) {
        const workletUrl = new URL("mic-worklet.js?v=0.2.0", document.baseURI);
        EW.micWorkletLoaded = context.audioWorklet.addModule(workletUrl.href).catch((error) => {
          EW.micWorkletLoaded = null;
          throw error;
        });
      }
      await EW.micWorkletLoaded;
      const worklet = new AudioWorkletNode(context, "echoweave-mic-processor", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: "explicit",
      });
      worklet.port.onmessage = (event) => {
        if (event.data?.type === "pcm16" && event.data.pcm instanceof ArrayBuffer) {
          sendMicrophonePCM(new Uint8Array(event.data.pcm));
        }
      };
      return worklet;
    } catch (error) {
      EW.micWorkletLoaded = null;
      addActivity(`AudioWorklet 不可用，已切换兼容采集 · ${error.name || "Error"}`, "warning");
    }
  }

  if (typeof context.createScriptProcessor !== "function") {
    throw new Error("浏览器不支持可用的实时音频处理接口");
  }
  const processor = context.createScriptProcessor(2048, 1, 1);
  processor.onaudioprocess = (event) => {
    const raw = event.inputBuffer.getChannelData(0);
    const downsampled = resample(raw, context.sampleRate, 16000);
    sendMicrophonePCM(floatToPCM16(downsampled));
  };
  return processor;
}

async function ensureAudioContext() {
  if (!EW.audioContext) {
    EW.audioContext = new AudioContext({ latencyHint: "interactive" });
  }
  if (EW.audioContext.state === "suspended") {
    await EW.audioContext.resume();
  }
  return EW.audioContext;
}

async function startMicrophone() {
  if (EW.micStream) return;
  if (EW.micPending) {
    await EW.micPending;
    if (
      EW.micStream ||
      !EW.microphoneRequested ||
      !EW.started ||
      EW.ws?.readyState !== WebSocket.OPEN
    ) {
      return;
    }
  }
  const requestEpoch = EW.micRequestEpoch;
  const pending = (async () => {
    const context = await ensureAudioContext();
    if (
      requestEpoch !== EW.micRequestEpoch ||
      !EW.microphoneRequested ||
      !EW.started ||
      EW.ws?.readyState !== WebSocket.OPEN
    ) {
      return;
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    if (
      requestEpoch !== EW.micRequestEpoch ||
      !EW.microphoneRequested ||
      !EW.started ||
      EW.ws?.readyState !== WebSocket.OPEN
    ) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    let source;
    let processor;
    let silentGain;
    try {
      source = context.createMediaStreamSource(stream);
      processor = await createMicrophoneProcessor(context);
      if (
        requestEpoch !== EW.micRequestEpoch ||
        !EW.microphoneRequested ||
        !EW.started ||
        EW.ws?.readyState !== WebSocket.OPEN
      ) {
        processor?.port?.postMessage({ type: "stop" });
        if (processor && "onaudioprocess" in processor) {
          processor.onaudioprocess = null;
        }
        processor?.disconnect();
        source?.disconnect();
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      silentGain = context.createGain();
      silentGain.gain.value = 0;
      source.connect(processor);
      processor.connect(silentGain);
      silentGain.connect(context.destination);
      EW.micStream = stream;
      EW.micProcessor = processor;
      EW.micSource = source;
      EW.micSilentGain = silentGain;
    } catch (error) {
      processor?.disconnect();
      source?.disconnect();
      silentGain?.disconnect();
      stream.getTracks().forEach((track) => track.stop());
      throw error;
    }
  })();
  EW.micPending = pending;
  try {
    await pending;
  } finally {
    if (EW.micPending === pending) EW.micPending = null;
  }
}

function stopMicrophone() {
  EW.micRequestEpoch += 1;
  EW.micProcessor?.port?.postMessage({ type: "stop" });
  if (EW.micProcessor && "onaudioprocess" in EW.micProcessor) {
    EW.micProcessor.onaudioprocess = null;
  }
  EW.micProcessor?.disconnect();
  EW.micSource?.disconnect();
  EW.micSilentGain?.disconnect();
  EW.micProcessor = null;
  EW.micSource = null;
  EW.micSilentGain = null;
  EW.micStream?.getTracks().forEach((track) => track.stop());
  EW.micStream = null;
  EW.micDroppedFrames = 0;
}

async function playPCM(payload, sampleRate) {
  if (
    !(payload instanceof ArrayBuffer) ||
    payload.byteLength === 0 ||
    payload.byteLength % 2 !== 0 ||
    payload.byteLength > MAX_TTS_FRAME_BYTES ||
    !Number.isInteger(sampleRate) ||
    sampleRate < 8000 ||
    sampleRate > 96000 ||
    EW.ttsChannels !== 1
  ) {
    addActivity("已丢弃不符合 PCM16 单声道约束的音频帧", "error");
    return;
  }
  const playoutEpoch = EW.playoutEpoch;
  const context = await ensureAudioContext();
  if (playoutEpoch !== EW.playoutEpoch) return;
  if (EW.scheduledAt - context.currentTime > MAX_AUDIO_BACKLOG_SECONDS) {
    for (const queuedSource of EW.playbackSources) {
      try { queuedSource.stop(); } catch (_) { /* already stopped */ }
    }
    EW.playbackSources.clear();
    EW.scheduledAt = context.currentTime;
    EW.audioDroppedFrames += 1;
    if (EW.audioDroppedFrames === 1 || EW.audioDroppedFrames % 10 === 0) {
      addActivity("音频播放积压已裁剪，优先保持实时性", "warning");
    }
  }
  const samples = new Int16Array(payload);
  if (!samples.length) return;
  const buffer = context.createBuffer(1, samples.length, sampleRate);
  const channel = buffer.getChannelData(0);
  let energy = 0;
  for (let i = 0; i < samples.length; i += 1) {
    channel[i] = samples[i] / 32768;
    energy += Math.abs(channel[i]);
  }
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(context.destination);
  const startAt = Math.max(context.currentTime + 0.025, EW.scheduledAt);
  source.start(startAt);
  EW.scheduledAt = startAt + buffer.duration;
  EW.playbackSources.add(source);
  avatar.classList.add("speaking");
  source.onended = () => {
    EW.playbackSources.delete(source);
    if (!EW.playbackSources.size) avatar.classList.remove("speaking");
  };
  const average = energy / samples.length;
  $("#mouth").style.height = `${Math.min(18, 4 + average * 28)}px`;
}

function clearPlayout() {
  EW.playoutEpoch += 1;
  EW.ttsFormatValid = false;
  EW.audioProtocolErrors = 0;
  window.speechSynthesis?.cancel();
  for (const source of EW.playbackSources) {
    try { source.stop(); } catch (_) { /* already stopped */ }
  }
  EW.playbackSources.clear();
  EW.audioDroppedFrames = 0;
  EW.scheduledAt = EW.audioContext?.currentTime || 0;
  EW.videoQueue.length = 0;
  EW.syncAudioTurns.clear();
  avatarVideo.onended = null;
  avatarVideo.onerror = null;
  avatarVideo.onloadeddata = null;
  avatarVideo.pause();
  if (EW.currentVideoUrl) {
    URL.revokeObjectURL(EW.currentVideoUrl);
    EW.currentVideoUrl = null;
  }
  avatarVideo.removeAttribute("src");
  avatarVideo.load();
  EW.playingVideo = false;
  avatarVideo.classList.remove("visible");
  avatar.classList.remove("speaking");
}

function browserSpeak(text) {
  if (!("speechSynthesis" in window) || !text) return;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "zh-CN";
  utterance.rate = 1.04;
  utterance.pitch = 1;
  utterance.onstart = () => avatar.classList.add("speaking");
  utterance.onend = () => {
    if (!window.speechSynthesis.speaking) avatar.classList.remove("speaking");
  };
  window.speechSynthesis.speak(utterance);
}

function queueVideo(data, turnId, ptsMs) {
  if (data.byteLength > MAX_VIDEO_BYTES) {
    showToast("视频分片超过客户端安全上限，已丢弃。");
    return;
  }
  while (EW.videoQueue.length >= MAX_VIDEO_QUEUE) {
    EW.videoQueue.shift();
  }
  const synchronizedAudio = EW.syncAudioTurns.get(turnId) || null;
  if (synchronizedAudio) EW.syncAudioTurns.delete(turnId);
  EW.videoQueue.push({
    blob: new Blob([data], { type: "video/mp4" }),
    synchronizedAudio,
    ptsMs,
  });
  if (!EW.playingVideo) playNextVideo();
}

function playNextVideo() {
  const item = EW.videoQueue.shift();
  if (!item) {
    EW.playingVideo = false;
    avatarVideo.classList.remove("visible");
    return;
  }
  EW.playingVideo = true;
  const playoutEpoch = EW.playoutEpoch;
  if (reducedMotionPreference?.matches) {
    avatarVideo.classList.remove("visible");
    (async () => {
      if (item.synchronizedAudio) {
        for (const frame of item.synchronizedAudio.frames) {
          if (playoutEpoch !== EW.playoutEpoch) return;
          await playPCM(frame, item.synchronizedAudio.sampleRate);
        }
      }
      if (playoutEpoch === EW.playoutEpoch) playNextVideo();
    })().catch(() => {
      if (playoutEpoch === EW.playoutEpoch) playNextVideo();
    });
    return;
  }
  const url = URL.createObjectURL(item.blob);
  EW.currentVideoUrl = url;
  avatarVideo.muted = true;
  avatarVideo.classList.add("visible");
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    URL.revokeObjectURL(url);
    if (EW.currentVideoUrl === url) EW.currentVideoUrl = null;
    playNextVideo();
  };
  avatarVideo.onended = finish;
  avatarVideo.onerror = finish;
  avatarVideo.onloadeddata = async () => {
    avatarVideo.onloadeddata = null;
    if (playoutEpoch !== EW.playoutEpoch) return;
    if (item.synchronizedAudio) {
      for (const frame of item.synchronizedAudio.frames) {
        if (playoutEpoch !== EW.playoutEpoch) return;
        await playPCM(frame, item.synchronizedAudio.sampleRate);
      }
    }
    if (playoutEpoch !== EW.playoutEpoch) return;
    avatarVideo.play().catch(finish);
  };
  avatarVideo.src = url;
  avatarVideo.load();
}

function sendStartControl() {
  if (EW.ws?.readyState !== WebSocket.OPEN) return null;
  if (EW.startSent) return EW.startAttempt;
  const personaId = personaInput.value.trim().toLowerCase();
  if (!/^[a-z0-9_-]+$/.test(personaId)) {
    showToast("PERSONA ID 只能包含小写字母、数字、下划线和连字符。");
    return null;
  }
  if (accessTokenInput.value.length > MAX_SESSION_TOKEN_CHARS) {
    showToast("会话令牌长度异常，请重新签发短期令牌。");
    accessTokenInput.focus();
    return null;
  }
  personaInput.value = personaId;
  EW.startAttempt += 1;
  EW.startSent = true;
  sendControl({
    type: "start",
    persona_id: personaId,
    access_token: accessTokenInput.value,
    ai_disclosure_ack: disclosureAck.checked,
    protocol: {
      version: 1,
      capabilities: CLIENT_CAPABILITIES,
    },
  });
  return EW.startAttempt;
}

function handleEvent(event) {
  trackSequence(event);
  switch (event.type) {
    case "session.hello":
      EW.helloReceived = true;
      EW.fatalError = false;
      renderRuntimeBackends(event.runtime?.backends || {});
      addActivity("网关握手完成，正在协商实时能力", "success");
      sendStartControl();
      break;
    case "session.negotiated":
      renderCapabilities(Array.isArray(event.capabilities) ? event.capabilities : []);
      addActivity(
        `协议 v${event.protocol_version || 1} 已协商 · ${EW.negotiatedCapabilities.length} 项能力`,
        "success",
      );
      if (Array.isArray(event.unavailable_capabilities) && event.unavailable_capabilities.length) {
        addActivity(`${event.unavailable_capabilities.length} 项能力由当前部署降级`, "warning");
      }
      break;
    case "session.pong": {
      const echoedAt = Number(event.client_time_ms);
      if (Number.isFinite(echoedAt)) {
        EW.lastRttMs = Math.max(0, Date.now() - echoedAt);
        EW.lastPongAt = Date.now();
        renderNetwork(EW.lastRttMs);
      }
      break;
    }
    case "session.ready":
      EW.started = true;
      EW.fatalError = false;
      EW.retryableError = false;
      EW.personaName = event.persona || "AI";
      connectionDot.classList.add("online");
      renderNetwork(EW.lastRttMs);
      setRecovery();
      startButton.innerHTML = `<span class="mic-icon"></span>实时对话已开启`;
      startButton.disabled = true;
      endButton.disabled = false;
      personaInput.disabled = true;
      accessTokenInput.disabled = true;
      $("#identityName").textContent = EW.personaName.toUpperCase();
      $("#identityType").textContent = event.fictional
        ? "虚构演示角色"
        : "已授权 AI 数字分身";
      avatar.setAttribute("aria-label", `AI 合成角色 ${EW.personaName} 的抽象头像`);
      addSystemNote(event.disclosure);
      addActivity(`${EW.personaName} 会话已就绪`, "success");
      browserSpeak(event.disclosure);
      if (EW.microphoneRequested) {
        startMicrophone()
          .then(() => {
            if (!EW.started) return;
            startButton.disabled = false;
            if (EW.microphoneRequested && EW.micStream) {
              startButton.innerHTML = `<span class="mic-icon"></span>关闭麦克风`;
              addActivity("麦克风已开启，可随时手动关闭", "success");
            } else {
              EW.microphoneRequested = false;
              startButton.innerHTML = `<span class="mic-icon"></span>开启麦克风`;
            }
          })
          .catch((error) => {
            EW.microphoneRequested = false;
            startButton.disabled = false;
            startButton.innerHTML = `<span class="mic-icon"></span>开启麦克风`;
            showToast(`麦克风不可用，已保留文字模式：${error.message}`);
          });
      } else {
        startButton.disabled = false;
        startButton.innerHTML = `<span class="mic-icon"></span>开启麦克风`;
      }
      break;
    case "session.state":
      if (["user_speaking", "transcribing", "thinking"].includes(event.state)) {
        EW.ttsFormatValid = false;
        EW.audioProtocolErrors = 0;
      }
      setState(event.state);
      break;
    case "asr.final":
      addMessage("user", event.text);
      setText($("#latencyLabel"), `ASR ${event.latency_ms} MS`);
      renderPipeline("llm", "asr");
      break;
    case "assistant.delta":
      renderPipeline("llm", "asr");
      appendAssistantDelta(event.text);
      break;
    case "assistant.final":
      finishAssistantStream(event.text);
      announceTranscript(`${EW.personaName}：${event.text}`);
      break;
    case "tts.format":
      if (
        !Number.isInteger(event.sample_rate) ||
        event.sample_rate < 8000 ||
        event.sample_rate > 96000 ||
        event.channels !== 1 ||
        event.codec !== "pcm_s16le"
      ) {
        EW.ttsFormatValid = false;
        clearPlayout();
        addActivity("服务端返回了不受支持的音频格式", "error");
        showToast("音频格式不受支持，已安全停止播放。");
        break;
      }
      EW.ttsSampleRate = event.sample_rate;
      EW.ttsChannels = event.channels;
      EW.ttsFormatValid = true;
      renderPipeline("tts", "llm");
      break;
    case "tts.browser":
      browserSpeak(event.text);
      break;
    case "av.sync_begin":
      if (
        !EW.ttsFormatValid ||
        event.sample_rate !== EW.ttsSampleRate ||
        event.channels !== EW.ttsChannels ||
        event.codec !== "pcm_s16le"
      ) {
        addActivity("音画同步格式与已协商音频不一致，已拒绝缓存", "error");
        break;
      }
      renderPipeline("avatar", "tts");
      if (EW.syncAudioTurns.size >= MAX_VIDEO_QUEUE) {
        EW.syncAudioTurns.delete(EW.syncAudioTurns.keys().next().value);
      }
      EW.syncAudioTurns.set(event.turn_id, {
        frames: [],
        bytes: 0,
        sampleRate: event.sample_rate,
      });
      break;
    case "avatar.segment":
      renderPipeline("avatar", "tts");
      break;
    case "turn.metrics":
      setText(
        firstTokenLatency,
        Number.isFinite(event.first_token_ms) ? `${event.first_token_ms} ms` : "— ms",
      );
      setText(
        turnLatency,
        Number.isFinite(event.end_to_end_ms) ? `${event.end_to_end_ms} ms` : "— ms",
      );
      renderPipeline(null, "avatar");
      addActivity(
        `本轮完成 · 首字 ${event.first_token_ms ?? "—"} ms · 全链路 ${event.end_to_end_ms ?? "—"} ms`,
        "success",
      );
      break;
    case "playout.clear":
      clearPlayout();
      if (!event.preserve_transcript) {
        finishAssistantStream();
      }
      break;
    case "turn.cancelled":
      clearPlayout();
      renderPipeline();
      addActivity("当前回答已安全打断", "warning");
      announceTranscript("当前回答已打断");
      finishAssistantStream();
      break;
    case "degraded":
      renderPipeline(null, null, String(event.component || "").toLowerCase());
      addActivity(`${event.component} 已切换到 ${event.fallback}`, "warning");
      showToast(`${event.component} 已降级到 ${event.fallback}：${event.reason}`);
      break;
    case "error":
      EW.fatalError = event.fatal === true;
      EW.retryableError = event.retryable === true;
      if (
        ["authentication_failed", "session_start_rejected", "disclosure_not_acknowledged"].includes(
          event.code,
        )
      ) {
        EW.startSent = false;
        EW.startAttempt += 1;
        EW.microphoneRequested = false;
        stopMicrophone();
        startButton.disabled = false;
        personaInput.disabled = false;
        accessTokenInput.disabled = false;
      }
      if (event.retryable && !EW.started) {
        EW.startSent = false;
        EW.startAttempt += 1;
      }
      addActivity(
        `${event.code}${event.error_id ? ` · ${event.error_id}` : ""}`,
        event.fatal ? "error" : "warning",
      );
      if (event.retryable || event.fatal) {
        setRecovery(event.retryable ? "重新建立安全会话" : "返回并检查配置", true);
      }
      showToast(`${event.code}：${event.message}`);
      break;
    default:
      break;
  }
}

function openSocket() {
  if (EW.ws?.readyState === WebSocket.OPEN) return Promise.resolve();
  if (
    EW.ws?.readyState === WebSocket.CONNECTING &&
    EW.startPending
  ) {
    return EW.startPending;
  }
  if (EW.ws && EW.ws.readyState !== WebSocket.OPEN) {
    const staleSocket = EW.ws;
    staleSocket.onopen = null;
    staleSocket.onclose = null;
    staleSocket.onerror = null;
    staleSocket.onmessage = null;
    try { staleSocket.close(); } catch (_) { /* already closed */ }
    EW.ws = null;
    EW.startPending = false;
    EW.startSent = false;
    EW.startAttempt += 1;
    EW.started = false;
    EW.helloReceived = false;
    EW.microphoneRequested = false;
    connectionDot.classList.remove("online");
    stopHeartbeat(staleSocket);
    clearPlayout();
    finishAssistantStream();
    stopMicrophone();
  }
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  EW.startPending = new Promise((resolve, reject) => {
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    let settled = false;
    const resolveOpen = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    const rejectOpen = (error) => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    EW.ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      if (EW.ws !== ws) return;
      connectionDot.classList.add("online");
      renderNetwork();
      addActivity(
        encryptedPage
          ? "WSS 加密实时通道已建立"
          : loopbackPage
            ? "本机实时通道已建立（未经过公网）"
            : "警告：当前实时通道未加密",
        encryptedPage || loopbackPage ? "success" : "error",
      );
      startHeartbeat(ws);
      resolveOpen();
    };
    ws.onmessage = async (message) => {
      if (EW.ws !== ws) return;
      if (typeof message.data === "string") {
        try {
          const event = JSON.parse(message.data);
          if (!event || typeof event !== "object" || Array.isArray(event)) {
            throw new Error("控制事件不是 JSON 对象");
          }
          handleEvent(event);
        } catch (error) {
          addActivity("收到无法解析的控制事件", "error");
          showToast(`协议事件无效：${error.message}`);
        }
        return;
      }
      try {
        const packet = parsePacket(message.data);
        if (packet.kind === 2) {
          if (!EW.ttsFormatValid) {
            EW.audioProtocolErrors += 1;
            if (EW.audioProtocolErrors === 1) {
              addActivity("收到未声明格式的音频帧，已安全丢弃", "error");
            }
          } else {
            const sync = EW.syncAudioTurns.get(packet.turnId);
            if (sync) {
              const nextBytes = sync.bytes + packet.payload.byteLength;
              if (nextBytes <= MAX_SYNC_AUDIO_BYTES) {
                sync.frames.push(packet.payload);
                sync.bytes = nextBytes;
              } else {
                EW.syncAudioTurns.delete(packet.turnId);
                sync.frames.length = 0;
                addActivity("音画同步缓存超过上限，已降级为低延迟音频", "warning");
                await playPCM(packet.payload, sync.sampleRate);
              }
            } else {
              await playPCM(packet.payload, EW.ttsSampleRate);
            }
          }
        }
        if (packet.kind === 3) {
          queueVideo(packet.payload, packet.turnId, packet.ptsMs);
        }
      } catch (error) {
        showToast(error.message);
      }
    };
    ws.onerror = () => {
      if (EW.ws === ws) EW.startPending = false;
      rejectOpen(new Error("无法连接 EchoWeave 网关"));
    };
    ws.onclose = (event) => {
      if (EW.ws !== ws) return;
      rejectOpen(new Error(`EchoWeave 网关已关闭连接（${event.code}）`));
      EW.started = false;
      EW.ws = null;
      EW.startPending = false;
      EW.helloReceived = false;
      EW.startSent = false;
      EW.startAttempt += 1;
      EW.microphoneRequested = false;
      connectionDot.classList.remove("online");
      stopHeartbeat(ws);
      renderCapabilities();
      startButton.disabled = false;
      endButton.disabled = true;
      personaInput.disabled = false;
      accessTokenInput.disabled = false;
      startButton.innerHTML = `<span class="mic-icon"></span>重新连接`;
      setState("closed");
      setRecovery(
        EW.fatalError && !EW.retryableError ? "检查会话配置" : "重新建立安全会话",
        true,
      );
      addActivity(`连接已关闭 · ${event.code}${event.reason ? ` · ${event.reason}` : ""}`, "error");
      clearPlayout();
      finishAssistantStream();
      stopMicrophone();
    };
  });
  return EW.startPending;
}

async function beginConversation() {
  if (EW.started && (EW.microphoneRequested || EW.micStream || EW.micPending)) {
    EW.microphoneRequested = false;
    stopMicrophone();
    startButton.disabled = false;
    startButton.innerHTML = `<span class="mic-icon"></span>开启麦克风`;
    addActivity("麦克风已由你手动关闭", "success");
    announceTranscript("麦克风已关闭");
    return;
  }
  if (!disclosureAck.checked) {
    showToast("请先确认你知道对方是 AI 数字分身。");
    return;
  }
  if (!/^[a-z0-9_-]+$/.test(personaInput.value.trim().toLowerCase())) {
    showToast("请先填写有效的 PERSONA ID。");
    return;
  }
  if (startButton.disabled) return;
  startButton.disabled = true;
  EW.microphoneRequested = true;
  try {
    await ensureAudioContext();
    if (EW.started) {
      await startMicrophone();
      startButton.disabled = false;
      startButton.innerHTML = EW.micStream
        ? `<span class="mic-icon"></span>关闭麦克风`
        : `<span class="mic-icon"></span>开启麦克风`;
      if (EW.micStream) addActivity("麦克风已开启，可随时手动关闭", "success");
      return;
    }
    await openSocket();
    sendStartControl();
  } catch (error) {
    EW.microphoneRequested = false;
    showToast(error.message);
    startButton.disabled = false;
    if (EW.started) {
      startButton.innerHTML = `<span class="mic-icon"></span>开启麦克风`;
    }
  }
}

async function sendTextTurn() {
  if (EW.textPending) {
    showToast("上一条消息仍在等待会话启动。");
    return;
  }
  const text = textInput.value.trim();
  if (!text) return;
  if (!disclosureAck.checked) {
    showToast("请先确认 AI 身份提示。");
    return;
  }
  EW.textPending = true;
  try {
    await ensureAudioContext();
    await openSocket();
    const attempt = sendStartControl();
    if (attempt === null) throw new Error("会话尚未建立");
    const waitUntilReady = () => new Promise((resolve, reject) => {
      const deadline = performance.now() + 5000;
      const check = () => {
        if (EW.startAttempt !== attempt) {
          reject(new Error("会话启动已失效，请重试"));
          return;
        }
        if (EW.started) {
          resolve();
          return;
        }
        if (performance.now() >= deadline) {
          reject(new Error("会话启动超时"));
          return;
        }
        setTimeout(check, 20);
      };
      check();
    });
    await waitUntilReady();
    if (!sendControl({ type: "text", text })) {
      throw new Error("连接正在关闭，消息未发送");
    }
    addMessage("user", text);
    if (textInput.value.trim() === text) textInput.value = "";
  } catch (error) {
    showToast(error.message);
  } finally {
    EW.textPending = false;
  }
}

function endConversation() {
  const socket = EW.ws;
  EW.microphoneRequested = false;
  stopMicrophone();
  clearPlayout();
  finishAssistantStream();
  stopHeartbeat(socket);
  EW.started = false;
  endButton.disabled = true;
  cancelButton.disabled = true;
  startButton.disabled = true;
  startButton.innerHTML = `<span class="mic-icon"></span>正在结束会话`;
  addActivity("你已结束会话，麦克风与媒体缓冲已释放", "success");
  announceTranscript("会话已结束");
  if (socket?.readyState === WebSocket.OPEN) {
    try { socket.send(JSON.stringify({ type: "stop" })); } catch (_) { /* closing */ }
    try { socket.close(1000, "client_stop"); } catch (_) { /* closing */ }
  }
  if (EW.ws === socket) EW.ws = null;
  EW.startPending = false;
  EW.helloReceived = false;
  EW.startSent = false;
  EW.startAttempt += 1;
  renderCapabilities();
  setRecovery();
  startButton.disabled = false;
  startButton.innerHTML = `<span class="mic-icon"></span>重新连接`;
  personaInput.disabled = false;
  accessTokenInput.disabled = false;
  setState("closed");
}

async function recoverConversation() {
  if (EW.fatalError && !EW.retryableError) {
    personaInput.disabled = false;
    accessTokenInput.disabled = false;
    accessTokenInput.focus();
    showToast("请检查 Persona、访问令牌和授权配置后重试。");
    return;
  }
  if (!disclosureAck.checked) {
    showToast("重新连接前，请先确认 AI 身份提示。");
    return;
  }
  setRecovery("正在重新连接…", false);
  EW.fatalError = false;
  EW.retryableError = false;
  try {
    await openSocket();
    if (!EW.startSent && sendStartControl() === null) {
      throw new Error("会话配置无效");
    }
    addActivity("正在恢复实时会话", "neutral");
  } catch (error) {
    setRecovery("重新建立安全会话", true);
    showToast(error.message);
  }
}

startButton.addEventListener("click", beginConversation);
sendButton.addEventListener("click", sendTextTurn);
endButton.addEventListener("click", endConversation);
recoveryAction?.addEventListener("click", recoverConversation);
textInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") sendTextTurn();
});
cancelButton.addEventListener("click", () => {
  clearPlayout();
  sendControl({ type: "cancel" });
});
$("#clearButton").addEventListener("click", () => {
  finishAssistantStream();
  transcript.innerHTML = "";
  addSystemNote("本地界面记录已清空；服务端默认不持久化音视频。");
});
window.addEventListener("beforeunload", () => {
  stopHeartbeat(EW.ws);
  sendControl({ type: "stop" });
  EW.ws?.close();
  stopMicrophone();
});
