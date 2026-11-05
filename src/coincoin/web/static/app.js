import { AudioSequenceGate } from "/static/audio-protocol.js";

const startButton = document.querySelector("#start");
const stopButton = document.querySelector("#stop");
const statusLabel = document.querySelector("#status");
const messageLabel = document.querySelector("#message");
const meter = document.querySelector("#meter");
const transcriptItems = new Map();

let audioContext;
let microphone;
let audioSocket;
let uiSocket;
let playbackNode;
let finishFlush;
const sequenceGate = new AudioSequenceGate();

function waitForOpen(socket) {
  return new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("WebSocket connection failed")), {
      once: true,
    });
  });
}

function addEntry(id, text) {
  const item = document.createElement("li");
  item.textContent = text;
  const entries = document.querySelector(id);
  entries.prepend(item);
  while (entries.children.length > 20) entries.lastElementChild.remove();
}

function updateTranscript(event) {
  let item = transcriptItems.get(event.item_id);
  if (!item) {
    item = document.createElement("li");
    transcriptItems.set(event.item_id, item);
    document.querySelector("#transcripts").prepend(item);
  }
  const speaker = event.speaker === "user" ? "Vous : " : "Coin-Coin : ";
  item.textContent = speaker + (event.final ? event.text : item.textContent.slice(speaker.length) + event.text);
}

async function start() {
  startButton.disabled = true;
  messageLabel.textContent = "Autorisation du micro…";
  sequenceGate.discardThroughSequence("0");
  try {
    microphone = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    audioContext = new AudioContext({ sampleRate: 24000 });
    await audioContext.audioWorklet.addModule("/static/mic-worklet.js");
    await audioContext.audioWorklet.addModule("/static/play-worklet.js");
    await audioContext.resume();

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    audioSocket = new WebSocket(`${protocol}//${location.host}/ws/audio`);
    audioSocket.binaryType = "arraybuffer";
    await waitForOpen(audioSocket);
    const connectedAudioSocket = audioSocket;
    uiSocket = new WebSocket(`${protocol}//${location.host}/ws/ui`);
    await waitForOpen(uiSocket);

    playbackNode = new AudioWorkletNode(audioContext, "coincoin-play");
    playbackNode.connect(audioContext.destination);
    playbackNode.port.onmessage = ({ data }) => {
      if (data.type === "played") {
        uiSocket.send(JSON.stringify(data));
      } else if (data.type === "flushed") {
        uiSocket.send(JSON.stringify(data));
        finishFlush?.();
      }
    };
    audioSocket.addEventListener("message", ({ data }) => {
      if (audioSocket === connectedAudioSocket && data instanceof ArrayBuffer) {
        const decoded = sequenceGate.decode(data);
        if (decoded !== null) {
          playbackNode.port.postMessage({ type: "frame", buffer: decoded.pcm }, [decoded.pcm]);
        }
      }
    });

    uiSocket.addEventListener("message", ({ data }) => {
      const event = JSON.parse(data);
      if (event.type === "flush") {
        sequenceGate.discardThroughSequence(event.through_sequence);
        playbackNode.port.postMessage(event);
      } else if (event.type === "replaced") {
        messageLabel.textContent = "Une autre page a remplacé cette connexion.";
        void cleanup();
      } else if (event.type === "latency") {
        messageLabel.textContent = `Latence aller-retour : ${event.ms} ms`;
      } else if (event.type === "error") {
        messageLabel.textContent = event.message;
      } else if (event.type === "state") {
        const speaking = Boolean(event.speaking);
        statusLabel.dataset.speaking = String(speaking);
        statusLabel.textContent = event.state + (speaking ? " · parle" : "");
      } else if (event.type === "transcript") {
        updateTranscript(event);
      } else if (event.type === "action") {
        addEntry("#actions", (event.ok === false ? "Refus : " : "") + event.text);
      }
    });

    const source = audioContext.createMediaStreamSource(microphone);
    const micNode = new AudioWorkletNode(audioContext, "coincoin-mic");
    const silentGain = audioContext.createGain();
    silentGain.gain.value = 0;
    micNode.port.onmessage = ({ data }) => {
      if (data.type === "frame" && audioSocket.readyState === WebSocket.OPEN) {
        audioSocket.send(data.buffer);
      } else if (data.type === "level") {
        meter.value = data.rms;
      }
    };
    source.connect(micNode);
    micNode.connect(silentGain).connect(audioContext.destination);
    stopButton.disabled = false;
    messageLabel.textContent = `Micro actif · ${audioContext.sampleRate} Hz`;
  } catch (error) {
    await cleanup();
    startButton.disabled = false;
    messageLabel.textContent = error instanceof Error ? error.message : String(error);
  }
}

async function stopPlayback() {
  if (uiSocket?.readyState !== WebSocket.OPEN) return;
  await new Promise((resolve) => {
    const timeout = setTimeout(() => {
      finishFlush = undefined;
      resolve();
    }, 150);
    finishFlush = () => {
      clearTimeout(timeout);
      finishFlush = undefined;
      resolve();
    };
    uiSocket.send(JSON.stringify({ type: "stop" }));
  });
}

async function cleanup() {
  microphone?.getTracks().forEach((track) => track.stop());
  microphone = undefined;
  audioSocket?.close();
  uiSocket?.close();
  audioSocket = undefined;
  uiSocket = undefined;
  playbackNode?.disconnect();
  playbackNode = undefined;
  if (audioContext) {
    await audioContext.close();
    audioContext = undefined;
  }
  stopButton.disabled = true;
  startButton.disabled = false;
}

startButton.addEventListener("click", start);
stopButton.addEventListener("click", () => void stopPlayback());
