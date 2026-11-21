// voiceMode.js — 🎙️ Conversational voice mode for Odysseus (craftale layer).
// Chains the existing building blocks into a continuous loop:
//   mic → STT (voiceRecorder) → auto-send → response → TTS autoplay (aiTTSManager) → mic …
// Self-contained: injects its own toggle button, observes completed responses
// via MutationObserver (no chat.js edits). Toggle again or Esc to leave.
// Providers must be enabled (Settings → AI → Text-to-Speech / Speech-to-Text),
// otherwise the button stays hidden with a tooltip explaining why.

import * as voiceRecorder from './voiceRecorder.js';

const VM = {
  active: false,
  speaking: false,
  btn: null,
  statusEl: null,
  lastPlayed: null,
  observer: null,
};

function ttsAvailable() {
  return window.aiTTSManager && window.aiTTSManager.available && window.aiTTSManager._provider !== 'disabled';
}

function sttAvailable() {
  return voiceRecorder && typeof voiceRecorder.startRecording === 'function'
    && !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

function setStatus(text) {
  if (VM.statusEl) VM.statusEl.textContent = text;
}

function sendFromTranscript(text) {
  const ta = document.getElementById('message');
  if (!ta || !text) return;
  ta.value = text;
  ta.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true,
  }));
  setStatus('💭 pensando…');
}

async function speakResponse() {
  // Find the latest assistant message text via the tts-button marker that
  // chat.js attaches when a response completes.
  const btns = document.querySelectorAll('.ai-tts-button');
  const last = btns[btns.length - 1];
  if (!last) return;
  if (VM.lastPlayed === last) return;            // already spoken
  VM.lastPlayed = last;
  const bubble = last.closest('[class*="message"], [class*="bubble"], article, div');
  const text = bubble ? bubble.textContent : '';
  if (!text || !ttsAvailable()) { setStatus('🎙️ ti ascolto…'); startListening(); return; }
  VM.speaking = true;
  setStatus('🔊 sto leggendo…');
  try { await window.aiTTSManager.play(text.slice(0, 4000)); }
  catch (e) { console.warn('[voiceMode] tts:', e); }
  VM.speaking = false;
  if (VM.active) { setStatus('🎙️ ti ascolto…'); startListening(); }
}

function startListening() {
  if (!VM.active || VM.speaking || voiceRecorder.getIsRecording()) return;
  voiceRecorder.startRecording(
    () => setStatus('📎 audio allegato — invialo tu'),
    (msg) => setStatus(msg || ''),
    (err) => { setStatus('⚠️ ' + (err || 'errore mic')); },
    (transcript) => sendFromTranscript((transcript || '').trim()),
  );
}

function onBodyMutation() {
  if (VM.active && !VM.speaking) speakResponse();
}

export function toggleVoiceMode() {
  VM.active = !VM.active;
  if (VM.btn) VM.btn.classList.toggle('vm-active', VM.active);
  if (VM.active) {
    setStatus('🎙️ ti ascolto… (Esc per uscire)');
    if (!VM.observer) {
      VM.observer = new MutationObserver(() => { clearTimeout(VM._t); VM._t = setTimeout(onBodyMutation, 400); });
      VM.observer.observe(document.getElementById('chat-container') || document.body, { childList: true, subtree: true });
    }
    document.addEventListener('keydown', VM._esc || (VM._esc = (e) => { if (e.key === 'Escape' && VM.active) toggleVoiceMode(); }));
    startListening();
  } else {
    setStatus('');
    if (VM.observer) { VM.observer.disconnect(); VM.observer = null; }
    if (voiceRecorder.getIsRecording()) voiceRecorder.stopRecording();
    if (window.aiTTSManager && window.aiTTSManager.currentAudio) { try { window.aiTTSManager.currentAudio.pause(); } catch (_) {} }
  }
}

function injectUI() {
  const ta = document.getElementById('message');
  if (!ta || VM.btn) return;
  const wrap = ta.parentElement;
  VM.btn = document.createElement('button');
  VM.btn.type = 'button';
  VM.btn.title = ttsAvailable() && sttAvailable()
    ? 'Voice mode: conversazione vocale continua'
    : 'Voice mode: serve STT+TTS abilitati (Settings → AI → sezioni Speech)';
  VM.btn.innerHTML = '🎙️';
  VM.btn.style.cssText = 'position:absolute;top:-30px;right:8px;border:none;background:transparent;cursor:pointer;font-size:17px;opacity:.55;transition:all .2s;z-index:5;';
  VM.btn.onclick = (e) => { e.preventDefault(); toggleVoiceMode(); };
  VM.statusEl = document.createElement('span');
  VM.statusEl.style.cssText = 'position:absolute;top:-30px;left:8px;font-size:11px;opacity:.6;z-index:5;';
  if (getComputedStyle(wrap).position === 'static') wrap.style.position = 'relative';
  wrap.appendChild(VM.btn);
  wrap.appendChild(VM.statusEl);
}

export function init() {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', injectUI);
  else injectUI();
  if (voiceRecorder.init) voiceRecorder.init();
}

init();
