import assert from "node:assert/strict";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

let setupChatModule;
let setupChatElementClass;

test("global chat launcher follows the authenticated main element across routes", async () => {
  const keys = ["HTMLElement", "customElements", "document", "MutationObserver", "setInterval", "window"];
  const descriptors = new Map(keys.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const intervals = [];
  const definitions = new Map();
  const children = [];
  const windowListeners = new Map();
  const windowStub = {
    location: {search: ""},
    dispatchEvent() {},
    addEventListener(type, callback, options) { windowListeners.set(type, {callback, options}); },
  };
  const main = { localName: "home-assistant-main", hass: { user: { id: "owner-one", is_admin: true }, locale: { language: "ru" } } };
  class HTMLElementStub {
    constructor() { this.shadowRoot = {}; }
    attachShadow() { return this.shadowRoot; }
    setAttribute() {}
    removeAttribute() {}
    remove() { this.parentNode?.removeChild(this); }
  }
  const body = {
    append(element) {
      if (element.parentNode) element.parentNode.removeChild(element);
      children.push(element); element.parentNode = this; element.isConnected = true;
    },
    removeChild(element) { const index = children.indexOf(element); if (index >= 0) children.splice(index, 1); element.parentNode = null; element.isConnected = false; },
  };
  Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: HTMLElementStub });
  Object.defineProperty(globalThis, "customElements", { configurable: true, value: {
    get: (name) => name === "home-assistant" ? HTMLElementStub : definitions.get(name),
    define: (name, value) => definitions.set(name, value),
  } });
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    documentElement: { dataset: {} }, body,
    querySelectorAll: () => [main],
    createElement: (name) => new (definitions.get(name))(),
  } });
  Object.defineProperty(globalThis, "MutationObserver", { configurable: true, value: class { constructor(callback) { this.callback = callback; } observe() {} } });
  Object.defineProperty(globalThis, "setInterval", { configurable: true, value: (callback) => { intervals.push(callback); return intervals.length; } });
  Object.defineProperty(globalThis, "window", { configurable: true, value: windowStub });
  try {
    setupChatModule = await import(`${pathToFileURL(resolve("habab/distribution/src/habab_home/frontend/setup-chat.js"))}?lifecycle-test`);
    intervals[0](); // wait for HA's custom element registry, then install the global launcher
    setupChatElementClass = definitions.get("habab-setup-chat");
    intervals[1]();
    assert.equal(children.length, 1);
    const launcher = children[0];
    assert.equal(launcher.hass, main.hass);
    assert.equal(launcher.autoOpenOnConnect, true, "the authorized admin assistant opens on its first mount");
    assert.equal(launcher.language, "ru");
    assert.equal(launcher.voiceMode, "habab", "the established Habab voice route remains the default");
    launcher.isConnected = true;
    const opener = { isConnected: true };
    const originalParams = { pipeline_id: "pipeline-original", start_listening: true };
    let forwarded;
    launcher.open = (...args) => { forwarded = args; return true; };
    const nativeDialogBridge = windowListeners.get("show-dialog");
    assert.equal(nativeDialogBridge.options, true, "native Assist rerouting runs in capture phase");
    const nativeOpener = Object.assign(new HTMLElementStub(), {isConnected: true, focus() {}});
    const makeDialogEvent = (detail, path = [nativeOpener]) => ({
      detail,
      composedPath: () => path,
      stopped: 0,
      prevented: 0,
      stopPropagation() { this.stopped++; },
      preventDefault() { this.prevented++; },
    });
    const nativeDialogEvent = makeDialogEvent({dialogTag: "ha-voice-command-dialog", dialogParams: originalParams});
    nativeDialogBridge.callback(nativeDialogEvent);
    assert.deepEqual(forwarded, [originalParams, nativeOpener]);
    assert.equal(nativeDialogEvent.stopped, 1);
    assert.equal(nativeDialogEvent.prevented, 1);
    const unrelatedDialogEvent = makeDialogEvent({dialogTag: "ha-config-flow-dialog", dialogParams: {}});
    nativeDialogBridge.callback(unrelatedDialogEvent);
    assert.equal(unrelatedDialogEvent.stopped, 0, "other HA dialogs keep their normal handler");
    assert.equal(unrelatedDialogEvent.prevented, 0);
    launcher.open = () => false;
    const rejectedDialogEvent = makeDialogEvent({dialogTag: "ha-voice-command-dialog", dialogParams: originalParams});
    nativeDialogBridge.callback(rejectedDialogEvent);
    assert.equal(rejectedDialogEvent.stopped, 0, "a declined chat open leaves the stock dialog available");
    assert.equal(rejectedDialogEvent.prevented, 0);
    launcher.open = (...args) => { forwarded = args; return true; };
    assert.equal(windowStub.hababAssistant.available, true);
    assert.equal(windowStub.hababAssistant.open(originalParams, opener), true);
    assert.deepEqual(forwarded, [originalParams, opener]);
    const protectedForm = {parentNode: null, draft: {api_key: "still-in-input"}};
    let detachCount = 0;
    const setupMount = {append(element) {
      if (element.parentNode) detachCount++;
      element.parentNode = this;
    }};
    setupChatModule.mountStableElement(setupMount, protectedForm);
    setupChatModule.mountStableElement(setupMount, protectedForm);
    assert.equal(detachCount, 0, "redrawing messages must not disconnect a protected integration draft");
    assert.equal(protectedForm.draft.api_key, "still-in-input");
    const history = [
      { role: "user", text: "Connect Xiaomi Home" },
      { role: "assistant", text: "" },
      { role: "assistant", text: "  \n" },
      { role: "assistant", text: "The setup card is ready." },
    ];
    assert.deepEqual(setupChatModule.visibleTextMessages(history), [history[0], history[3]]);
    assert.deepEqual(setupChatModule.assistantFeedbackContext(history, 3), {
      answer_index: 3,
      answer_text: "The setup card is ready.",
      prompt_text: "Connect Xiaomi Home",
    });
    assert.equal(setupChatModule.assistantFeedbackContext(history, 0), null, "user messages do not get answer feedback controls");
    assert.equal(setupChatModule.assistantFeedbackContext(history, 8), null, "out-of-range answers are rejected");
    const toolOnly = {role: "assistant", text: "", tool_calls: [{name: "bash", status: "error"}]};
    assert.deepEqual(setupChatModule.visibleTextMessages([toolOnly, {role: "user", text: ""}]), [toolOnly]);
    assert.equal(history.length, 4, "filtering display messages must retain session history");

    const createElement = document.createElement;
    const previousCurrentContext = launcher.isCurrentOwnerContext;
    const previousDraw = launcher.draw;
    class FeedbackElement {
      constructor(name) { this.localName = name; this.children = []; this.dataset = {}; this.attributes = {}; }
      append(...items) { this.children.push(...items); }
      setAttribute(name, value) { this.attributes[name] = value; }
    }
    document.createElement = (name) => new FeedbackElement(name);
    launcher.isCurrentOwnerContext = () => true;
    launcher.draw = () => {};
    launcher.messages = [{role: "user", text: "Question"}, {role: "assistant", text: "Answer"}];
    launcher.sessionId = "feedback-session";
    launcher.feedbackStates = new Map();
    const feedbackCopyKeys = [
      "feedbackPositive", "feedbackNegative", "feedbackPositiveLabel", "feedbackNegativeLabel",
      "feedbackSending", "feedbackSent", "feedbackSentTruncated", "feedbackSentWithoutTrace",
      "feedbackNotConfigured", "feedbackPending", "feedbackFailed",
    ];
    for (const key of feedbackCopyKeys) {
      assert.equal(typeof launcher.t[key], "string", `${key} must be provided by the real chat getter`);
      assert.notEqual(launcher.t[key].trim(), "", `${key} must not be empty`);
    }
    let feedbackRequests = 0;
    let finishFeedbackRequest;
    launcher.hass.fetchWithAuth = () => {
      feedbackRequests++;
      return new Promise((resolve) => { finishFeedbackRequest = resolve; });
    };
    const controls = launcher.renderAnswerFeedback(1);
    assert.equal(controls.children.length, 2, "each assistant answer has positive and negative buttons");
    assert.equal(controls.children[0].title, launcher.t.feedbackPositiveLabel);
    assert.equal(controls.children[0].attributes["aria-label"], launcher.t.feedbackPositiveLabel);
    assert.equal(controls.children[1].title, launcher.t.feedbackNegativeLabel);
    assert.equal(controls.children[1].attributes["aria-label"], launcher.t.feedbackNegativeLabel);
    const submission = controls.children[0].onclick();
    await Promise.resolve();
    assert.equal(launcher.renderAnswerFeedback(1).children[0].disabled, true, "both ratings lock while one is pending");
    await launcher.renderAnswerFeedback(1).children[1].onclick();
    assert.equal(feedbackRequests, 1, "a second click cannot submit the same answer twice");
    finishFeedbackRequest({ok: true, json: async () => ({
      receipt_id: "receipt", state: "saved_locally", delivery_status: "saved_on_server", trace_status: "available",
    })});
    await submission;
    const completedFeedback = launcher.renderAnswerFeedback(1);
    assert.equal(completedFeedback.children[0].disabled, true);
    assert.equal(completedFeedback.children[2].textContent, launcher.t.feedbackSent);
    assert.equal(typeof completedFeedback.children[2].textContent, "string");
    assert.notEqual(completedFeedback.children[2].textContent.trim(), "");
    document.createElement = createElement;
    launcher.isCurrentOwnerContext = previousCurrentContext;
    launcher.draw = previousDraw;

    main.route = { path: "/config/areas" };
    intervals[1]();
    assert.equal(children.length, 1);
    assert.equal(children[0], launcher);

    main.hass = { user: { id: "owner-one", is_admin: true }, locale: { language: "en" } };
    intervals[1]();
    assert.equal(launcher.language, "en");

    const sameOwnerHass = launcher.hass;
    main.hass = undefined;
    intervals[1]();
    assert.equal(children.length, 1, "an unknown context hides the existing chat without removing it");
    assert.equal(children[0], launcher);
    assert.equal(launcher.contextAvailable, false);
    assert.equal(launcher.hidden, true);
    assert.equal(launcher.inert, true);
    assert.equal(launcher.hass, sameOwnerHass, "the preserved chat does not receive an undefined hass value");
    assert.equal(windowStub.hababAssistant, undefined, "unknown owner context disables the assistant hook");
    const unknownDialogEvent = makeDialogEvent({dialogTag: "ha-voice-command-dialog", dialogParams: originalParams});
    nativeDialogBridge.callback(unknownDialogEvent);
    assert.equal(unknownDialogEvent.stopped, 0, "a stale launcher cannot consume native Assist while context is unknown");

    main.hass = { user: { id: "owner-one", is_admin: true }, locale: { language: "ru" } };
    intervals[1]();
    assert.equal(children[0], launcher, "the same owner reuses the same chat instance");
    assert.equal(launcher.contextAvailable, true);
    assert.equal(launcher.hidden, false);
    assert.equal(launcher.inert, false);
    assert.equal(windowStub.hababAssistant.available, true);

    main.hass = { user: { id: "owner-two", is_admin: true }, locale: { language: "ru" } };
    intervals[1]();
    assert.equal(children.length, 1);
    assert.notEqual(children[0], launcher, "a different administrator receives a fresh chat instance");
    assert.equal(children[0].ownerId, "owner-two");
    assert.equal(windowStub.hababAssistant.available, true);

    main.hass = { user: { id: "guest", is_admin: false }, locale: { language: "ru" } };
    intervals[1]();
    assert.equal(children.length, 0);
    assert.equal(windowStub.hababAssistant, undefined);
    const guestDialogEvent = makeDialogEvent({dialogTag: "ha-voice-command-dialog", dialogParams: originalParams});
    nativeDialogBridge.callback(guestDialogEvent);
    assert.equal(guestDialogEvent.stopped, 0, "a private non-admin event is left untouched");
    assert.equal(guestDialogEvent.prevented, 0);
    document.documentElement.dataset.hababDemo = "true";
    intervals[1]();
    assert.equal(children.length, 1);
    assert.equal(windowStub.hababAssistant, undefined, "the public demo must not expose the assistant hook");
    assert.equal(children[0].autoOpenOnConnect, true, "the public demo opens the same assistant on first mount");
    let demoForwarded;
    children[0].open = (...args) => { demoForwarded = args; return true; };
    const demoDialogEvent = makeDialogEvent({dialogTag: "ha-voice-command-dialog", dialogParams: originalParams});
    nativeDialogBridge.callback(demoDialogEvent);
    assert.deepEqual(demoForwarded, [originalParams, nativeOpener], "the public stock Assist launcher opens the same chat");
    assert.equal(demoDialogEvent.stopped, 1);
    assert.equal(demoDialogEvent.prevented, 1);
  } finally {
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test("assistant owner policy preserves unknown context but separates administrators", () => {
  const context = (id, isAdmin = true) => ({
    localName: "home-assistant-main",
    hass: {user: {id, is_admin: isAdmin}},
  });
  assert.deepEqual(setupChatModule.classifyAssistantContext(undefined, undefined), {state: "unknown"});
  assert.deepEqual(setupChatModule.classifyAssistantContext("owner-one", {localName: "home-assistant-main"}), {state: "unknown"});
  assert.deepEqual(setupChatModule.classifyAssistantContext("owner-one", {hass: {user: {id: "owner-one"}}}), {state: "unknown"});
  assert.deepEqual(setupChatModule.classifyAssistantContext("owner-one", context("owner-one")), {state: "admin", ownerId: "owner-one"});
  assert.deepEqual(setupChatModule.classifyAssistantContext("owner-one", context("owner-two")), {state: "different-user", ownerId: "owner-two"});
  assert.deepEqual(setupChatModule.classifyAssistantContext("owner-one", context("guest", false)), {state: "non-admin"});
  assert.deepEqual(setupChatModule.classifyAssistantContext(undefined, context("", true)), {state: "unknown"});

  const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  const main = context("owner-one");
  Object.defineProperty(globalThis, "document", {configurable: true, value: {querySelectorAll: () => [main]}});
  try {
    const oldChat = Object.create(setupChatElementClass.prototype);
    Object.assign(oldChat, {
      isConnected: false,
      contextAvailable: true,
      hidden: false,
      inert: false,
      ownerId: "owner-one",
      _hass: main.hass,
    });
    assert.equal(oldChat.isCurrentOwnerContext(), false, "a detached chat cannot resume work for a later owner context");
    oldChat.isConnected = true;
    assert.equal(oldChat.isCurrentOwnerContext(), true);
  } finally {
    if (documentDescriptor) Object.defineProperty(globalThis, "document", documentDescriptor);
    else delete globalThis.document;
  }
});

test("repeated available sync does not reactivate the integration card", () => {
  const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  const main = {localName: "home-assistant-main", hass: {user: {id: "owner-one", is_admin: true}}};
  Object.defineProperty(globalThis, "document", {configurable: true, value: {querySelectorAll: () => [main]}});
  const dialog = {open: true};
  let activeSetterCalls = 0;
  let assistantOpenCalls = 0;
  let controlsCalls = 0;
  let pendingAssistCalls = 0;
  const chat = Object.create(setupChatElementClass.prototype);
  Object.assign(chat, {
    contextAvailable: false,
    hidden: true,
    inert: true,
    ownerId: "owner-one",
    busy: false,
    isConnected: true,
    shadowRoot: {firstChild: {}, querySelector: () => dialog},
    removeAttribute() {},
    setAssistantOpen() { assistantOpenCalls++; },
    controls() { controlsCalls++; },
    startPendingAssist() { pendingAssistCalls++; },
    integrationCard: Object.defineProperty({}, "active", {
      set() { activeSetterCalls++; },
    }),
  });
  try {
    chat.setContextAvailable(true);
    chat.setContextAvailable(true);
    assert.equal(activeSetterCalls, 1, "the integration card activates only on context restoration");
    assert.equal(assistantOpenCalls, 1);
    assert.equal(controlsCalls, 1);
    assert.equal(pendingAssistCalls, 1);
    assert.equal(chat.contextAvailable, true);
    assert.equal(chat.hidden, false);
    assert.equal(chat.inert, false);
  } finally {
    if (documentDescriptor) Object.defineProperty(globalThis, "document", documentDescriptor);
    else delete globalThis.document;
  }
});

test("voice cleanup invalidates callbacks without clearing a pending text busy state", () => {
  let closeCalls = 0;
  const activeVoice = Object.create(setupChatElementClass.prototype);
  Object.assign(activeVoice, {
    voice: {close() { closeCalls++; }}, voiceBusy: true, busy: true,
    voiceGeneration: 8, pendingVoiceText: "voice transcript", voiceReplyPipeline: {tts_engine: "engine"},
    isConnected: false,
  });
  setupChatElementClass.prototype.cleanupVoice.call(activeVoice);
  assert.equal(closeCalls, 1);
  assert.equal(activeVoice.voice, undefined);
  assert.equal(activeVoice.voiceBusy, false);
  assert.equal(activeVoice.busy, false, "busy owned by the voice capture is released");
  assert.equal(activeVoice.pendingVoiceText, undefined);
  assert.equal(activeVoice.voiceReplyPipeline, undefined);
  assert.equal(activeVoice.voiceGeneration, 9, "late capture callbacks become stale");

  const pendingText = Object.create(setupChatElementClass.prototype);
  let playbackCloseCalls = 0;
  Object.assign(pendingText, {
    voice: {close() { playbackCloseCalls++; }}, voiceBusy: true, busy: true,
    running: true, voiceGeneration: 3, isConnected: false,
  });
  setupChatElementClass.prototype.cleanupVoice.call(pendingText);
  assert.equal(playbackCloseCalls, 1, "playback closes while a text turn is still running");
  assert.equal(pendingText.voiceBusy, false);
  assert.equal(pendingText.busy, true, "voice cleanup leaves a pending text request busy");
});

test("closing native Assist TTS releases its busy lease without letting a stale finalizer clear a reopened run", async () => {
  let closeCalls = 0;
  let speakCalls = 0;
  const chat = Object.create(setupChatElementClass.prototype);
  Object.assign(chat, {
    voice: {close() { closeCalls++; }}, voiceBusy: false, busy: true, running: false,
    voiceGeneration: 10, nativeAssistBusyGeneration: 10, isConnected: false,
    nativeAssistEndCue: Promise.resolve(), nativeAssistPipeline: {id: "pipeline"},
    speakReply: async () => { speakCalls++; }, isCurrentOwnerContext: () => true, controls() {},
  });
  chat.cleanupVoice();
  assert.equal(closeCalls, 1);
  assert.equal(chat.busy, false, "closing releases busy state owned by native TTS");
  assert.equal(chat.nativeAssistBusyGeneration, undefined);
  assert.equal(chat.voiceGeneration, 11);

  chat.busy = true;
  chat.nativeAssistBusyGeneration = 11;
  await chat.finishNativeAssistReply(10);
  assert.equal(chat.busy, true, "the old finalizer cannot clear the reopened run");
  assert.equal(chat.nativeAssistBusyGeneration, 11);
  assert.equal(speakCalls, 0, "the old completion cannot speak the reopened run's response");
  await chat.finishNativeAssistReply(11);
  assert.equal(chat.busy, false, "the current finalizer releases its own busy lease");
  assert.equal(chat.nativeAssistBusyGeneration, undefined);
  assert.equal(speakCalls, 1);
});

test("real open method preserves startup focus and desktop manual focus", () => {
  const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  const mediaDescriptor = Object.getOwnPropertyDescriptor(globalThis, "matchMedia");
  let dashboard = {inert: false};
  let assistantMain;
  Object.defineProperty(globalThis, "document", {configurable: true, value: {
    documentElement: {dataset: {}}, head: null,
    querySelector: () => dashboard,
    querySelectorAll: () => assistantMain ? [assistantMain] : [],
  }});
  Object.defineProperty(globalThis, "matchMedia", {configurable: true, value: () => ({matches: false})});
  const makeInstance = ({demo}) => {
    const calls = {textarea: 0, close: 0, attributes: new Set()};
    const textarea = {value: "", focus(options) { calls.textarea++; calls.textareaOptions = options; }};
    const close = {focus(options) { calls.close++; calls.closeOptions = options; }};
    const dialog = {open: false, show() { this.open = true; }};
    const instance = Object.create(setupChatElementClass.prototype);
    instance.isConnected = true;
    instance.demo = demo;
    instance._hass = {user: {is_admin: true}};
    if (!demo) {
      instance._hass.user.id = "owner-one";
      instance.ownerId = "owner-one";
      instance.contextAvailable = true;
      assistantMain = {localName: "home-assistant-main", hass: instance._hass};
    }
    instance.sessionId = "existing-session";
    instance.busy = true;
    instance.q = (selector) => selector === "textarea" ? textarea
      : selector === ".close" ? close
        : selector === ".launcher" ? close
          : selector === "dialog" ? dialog : undefined;
    instance.setAttribute = (name) => calls.attributes.add(name);
    instance.removeAttribute = (name) => calls.attributes.delete(name);
    instance.toggleAttribute = (name, enabled) => enabled ? calls.attributes.add(name) : calls.attributes.delete(name);
    return {instance, calls};
  };

  try {
    const startup = makeInstance({demo: true});
    assert.equal(setupChatElementClass.prototype.open.call(startup.instance, {}, null, {focus: false}), true);
    assert.equal(startup.calls.textarea, 0, "automatic open does not raise the keyboard");
    assert.equal(startup.calls.close, 1, "startup focus stays on the chat header");
    assert.equal(startup.calls.closeOptions.preventScroll, true);
    assert.equal(startup.calls.attributes.has("data-assistant-open"), true);
    assert.equal(document.documentElement.dataset.hababAssistantOpen, "true");
    assert.equal(startup.instance.focusComposerOnOpen, false, "connection completion must retain startup focus policy");

    startup.instance.setAssistantOpen(false);
    assert.equal(document.documentElement.dataset.hababAssistantOpen, undefined, "closing restores dashboard width");
    assert.equal(setupChatElementClass.prototype.open.call(startup.instance, {}, null), true);
    assert.equal(startup.calls.textarea, 1, "a later desktop open focuses the composer");
    assert.equal(startup.calls.textareaOptions.preventScroll, true);

    const native = makeInstance({demo: false});
    assert.equal(setupChatElementClass.prototype.open.call(native.instance, {}), true);
    assert.equal(native.calls.textarea, 1, "the native assistant keeps default desktop composer focus");
    assert.equal(native.calls.attributes.has("data-assistant-open"), true, "all assistant surfaces share the split state");
  } finally {
    if (documentDescriptor) Object.defineProperty(globalThis, "document", documentDescriptor);
    else delete globalThis.document;
    if (mediaDescriptor) Object.defineProperty(globalThis, "matchMedia", mediaDescriptor);
    else delete globalThis.matchMedia;
  }
});

test("mobile open starts split, textarea focus expands, and collapse restores the dashboard", () => {
  const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  const mediaDescriptor = Object.getOwnPropertyDescriptor(globalThis, "matchMedia");
  let dashboard = {inert: false};
  Object.defineProperty(globalThis, "document", {configurable: true, value: {
    documentElement: {dataset: {}}, head: null,
    querySelector: () => dashboard,
  }});
  Object.defineProperty(globalThis, "matchMedia", {configurable: true, value: () => ({matches: true})});
  const calls = {textarea: 0, blur: 0, close: 0, attributes: new Set()};
  const textarea = {value: "", focus() { calls.textarea++; }, blur() { calls.blur++; }};
  const close = {focus(options) { calls.close++; calls.closeOptions = options; }};
  const dialog = {open: false, show() { this.open = true; }};
  const instance = Object.create(setupChatElementClass.prototype);
  Object.assign(instance, {isConnected: true, demo: true, sessionId: "existing-session", busy: true, mobileExpanded: false});
  instance.q = (selector) => selector === "textarea" ? textarea
    : selector === ".close" ? close
      : selector === ".launcher" ? close
        : selector === "dialog" ? dialog : undefined;
  instance.setAttribute = (name) => calls.attributes.add(name);
  instance.removeAttribute = (name) => calls.attributes.delete(name);
  instance.toggleAttribute = (name, enabled) => enabled ? calls.attributes.add(name) : calls.attributes.delete(name);

  try {
    assert.equal(setupChatElementClass.prototype.open.call(instance), true);
    assert.equal(calls.textarea, 0, "mobile open does not raise the keyboard");
    assert.equal(instance.mobileExpanded, false);
    instance.setMobileExpanded(true);
    assert.equal(instance.mobileExpanded, true);
    assert.equal(dashboard.inert, true, "the dashboard is inert while the chat uses the full mobile viewport");
    instance.setMobileExpanded(true);
    assert.equal(instance.mobileInertOriginal, false, "repeated focus does not recapture the chat's own inert value");
    assert.equal(document.documentElement.dataset.hababAssistantExpanded, "true");
    instance.collapseMobileChat();
    assert.equal(calls.blur, 1);
    assert.equal(instance.mobileExpanded, false);
    assert.equal(dashboard.inert, false);
    assert.equal(document.documentElement.dataset.hababAssistantExpanded, undefined);
    assert.equal(calls.closeOptions.preventScroll, true);

    dashboard = {inert: true};
    const alreadyInert = Object.create(setupChatElementClass.prototype);
    Object.assign(alreadyInert, {demo: true, mobileExpanded: false});
    alreadyInert.q = (selector) => selector === "dialog" ? dialog : undefined;
    alreadyInert.toggleAttribute = () => {};
    alreadyInert.setMobileExpanded(true);
    alreadyInert.setMobileExpanded(false);
    assert.equal(dashboard.inert, true, "a pre-existing inert dashboard remains inert after collapse");
  } finally {
    if (documentDescriptor) Object.defineProperty(globalThis, "document", documentDescriptor);
    else delete globalThis.document;
    if (mediaDescriptor) Object.defineProperty(globalThis, "matchMedia", mediaDescriptor);
    else delete globalThis.matchMedia;
  }
});

test("explicit submitted prompts use the shared public chat transport", () => {
  const instance = Object.create(setupChatElementClass.prototype);
  const sent = [];
  Object.assign(instance, {
    sessionId: "public-demo", busy: false, demo: true,
    openParams: {submit_prompt: true, initial_prompt: "connect Zigbee", start_listening: true},
    q: (selector) => selector === "dialog" ? {open: true} : undefined,
    send: (...args) => sent.push(args),
    startVoice: () => assert.fail("public submitted prompts must not start the private voice path"),
  });
  setupChatElementClass.prototype.startPendingAssist.call(instance);
  assert.deepEqual(sent, [["connect Zigbee", {clearDraft: true}]]);
  assert.equal(instance.openParams.submit_prompt, false);
  assert.equal(instance.openParams.start_listening, true);
});

test("native pipeline selection honors an explicit ID and never falls back when it disappears", async () => {
  const pipelines = [{id: "one"}, {id: "two"}];
  assert.equal(setupChatModule.chooseAssistPipeline("two", pipelines, "one", "one"), pipelines[1]);
  assert.equal(setupChatModule.chooseAssistPipeline("last_used", pipelines, "one", "two"), pipelines[1]);
  assert.equal(setupChatModule.chooseAssistPipeline("preferred", pipelines, "two", "one"), pipelines[1]);
  assert.equal(setupChatModule.chooseAssistPipeline("removed-pipeline", pipelines, "one", undefined), undefined);
  assert.equal(setupChatModule.chooseAssistPipeline("last_used", [], undefined, undefined), undefined);
});

test("browser voice capability selects only explicit Habab realtime or HA Assist fallback", () => {
  const capability = {
    available: true, client: "browser_v1", provider: "habab", realtime_provider: "gemini",
    transport: "habab", websocket_url: "wss://habab-voice.wpmix.net/gemini/ws",
    auth: {mode: "first_text_frame", type: "habab.voice.auth.v1"},
  };
  assert.equal(setupChatModule.selectBrowserVoiceRoute(capability), "habab_browser_v1");
  assert.equal(setupChatModule.selectBrowserVoiceRoute({
    available: false, reason: "selected_provider_requires_ha_assist_stt",
    browser: {transport: "ha_assist_pipeline", requires_ha_stt: true},
  }), "ha_assist_pipeline");
  for (const unsupported of [
    {available: false, reason: "browser_voice_unavailable"},
    {...capability, provider: "openai"},
    {...capability, auth: {mode: "query", type: "habab.voice.auth.v1"}},
    {...capability, websocket_url: "wss://attacker.invalid/gemini/ws"},
    {available: false, reason: "selected_provider_requires_ha_assist_stt",
      browser: {transport: "ha_assist_pipeline", requires_ha_stt: false}},
  ]) assert.equal(setupChatModule.selectBrowserVoiceRoute(unsupported), undefined);
});

test("assistant diagnostics use only bounded safe fields and never provider response text", async () => {
  const copy = {diagnosticAuth: "Check your key.", diagnosticQuota: "Check your limits.", diagnosticGeneric: "Check settings."};
  assert.equal(setupChatModule.assistantDiagnosticMessage({name: "AuthenticationError", http_status: 401, response: "secret key"}, copy), "Check your key.");
  assert.equal(setupChatModule.assistantDiagnosticMessage({name: "other", http_status: 429, mentions_timeout: true}, copy), "Check your limits.");
  assert.equal(setupChatModule.assistantDiagnosticMessage({name: "other", raw: "provider secret"}, copy), "Check settings.");
});


test("quick replies append to a draft instead of replacing user text", () => {
  assert.equal(setupChatModule.appendQuickReplyToDraft("I have two lights  \n", "Can I add them?"), "I have two lights\nCan I add them?");
  assert.equal(setupChatModule.appendQuickReplyToDraft("  ", "I want Zigbee"), "I want Zigbee");
});

test("native Assist turn reuses the chat message shape without mutating loaded history", () => {
  const existing = [{role: "assistant", text: "Earlier answer"}];
  const updated = setupChatModule.appendNativeAssistTurn(existing, "Turn on the light", "Light is on");
  assert.deepEqual(existing, [{role: "assistant", text: "Earlier answer"}], "the loaded history stays unchanged");
  assert.deepEqual(updated, [
    {role: "assistant", text: "Earlier answer"},
    {role: "user", text: "Turn on the light"},
    {role: "assistant", text: "Light is on"},
  ]);
  assert.equal(setupChatModule.appendNativeAssistTurn(updated, "Recognized request", "" ).at(-1).role, "user");
});

test("reload protection covers active turns, voice, pending transcript, and unsent draft", () => {
  const shouldBlock = setupChatModule.shouldBlockFrontendReload;
  assert.equal(shouldBlock({busy: false, voiceBusy: false, activeVoice: false}), false);
  assert.equal(shouldBlock({busy: true}), true);
  assert.equal(shouldBlock({voiceBusy: true}), true);
  assert.equal(shouldBlock({activeVoice: true}), true);
  assert.equal(shouldBlock({pendingVoiceText: "turn on the light"}), true);
  assert.equal(shouldBlock({draft: "unsent question"}), true);
  assert.equal(shouldBlock({draft: "  \n"}), false);

  const states = [];
  const instance = Object.create(setupChatElementClass.prototype);
  const textarea = {value: "unsent question"};
  instance.demo = false;
  instance.busy = false;
  instance.voiceBusy = false;
  instance.voice = undefined;
  instance.pendingVoiceText = undefined;
  instance.shadowRoot = {querySelector: () => textarea};
  instance.reloadStateProvider = {setDirty: (value) => states.push(value)};
  instance.syncReloadState();
  textarea.value = "";
  instance.busy = true;
  instance.syncReloadState();
  instance.busy = false;
  instance.voiceBusy = true;
  instance.syncReloadState();
  instance.voiceBusy = false;
  instance.pendingVoiceText = "recognized request";
  instance.syncReloadState();
  instance.pendingVoiceText = undefined;
  instance.syncReloadState();
  assert.deepEqual(states, [true, true, true, true, false]);
});

test("private chat registers its own reload provider and releases it on disconnect", () => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  let registeredOwner;
  let unregistered = 0;
  const win = {
    haRegisterDirtyStateProvider(owner) {
      registeredOwner = owner;
      return {setDirty() {}, unregister() { unregistered++; }};
    },
    removeEventListener() {},
  };
  Object.defineProperty(globalThis, "window", {configurable: true, value: win});
  try {
    const instance = Object.create(setupChatElementClass.prototype);
    instance.demo = false;
    instance.shadowRoot = {firstChild: {} };
    instance.messages = [];
    instance.q = () => ({open: false});
    instance.subscribeVoiceHistory = () => {};
    instance.installLayoutLifecycle = () => {};
    instance.syncReloadState = () => {};
    instance.cleanupVoice = () => {};
    instance.setAssistantOpen = () => {};
    instance.connectedCallback();
    assert.equal(registeredOwner, instance);

    instance.disconnectedCallback();
    assert.equal(unregistered, 1);
    assert.equal(instance.reloadStateProvider, undefined);
  } finally {
    if (previousWindow) Object.defineProperty(globalThis, "window", previousWindow);
    else Reflect.deleteProperty(globalThis, "window");
  }
});

test("completed or failed Xiaomi cards stay removed after dismissal and return for a new active phase", () => {
  const presentation = setupChatModule.getXiaomiSetupPresentation;
  assert.deepEqual(presentation("completed", false), {
    showCard: false, clearDismissal: false,
  });
  const dismissed = presentation("completed", true);
  assert.deepEqual(dismissed, {
    showCard: false, clearDismissal: false,
  });
  assert.deepEqual(presentation("completed", true), dismissed, "completed cards never return after reload");
  assert.deepEqual(presentation("error", false), {
    showCard: true, clearDismissal: false,
  });
  assert.deepEqual(presentation("error", true), {
    showCard: false, clearDismissal: false,
  }, "a dismissed failed setup stays removed");
  assert.deepEqual(presentation("oauth", true), {
    showCard: true, clearDismissal: true,
  }, "a pending setup remains visible and clears an old completed dismissal");
  assert.deepEqual(presentation("ready", true), {
    showCard: true, clearDismissal: true,
  }, "starting another setup restores its card");
});

test("a new chat hides every prior Xiaomi and integration card while allowing a new flow", async () => {
  const setupCard = {hidden: false, state: {phase: "oauth", setup_id: "xiaomi-old"}};
  const integrationCard = {
    hidden: false,
    interactions: [{id: "active-setup", phase: "form"}, {id: "completed-setup", phase: "done"}],
    setSuppressedInteractionIds(ids) {
      this.suppressedInteractionIds = new Set(ids);
      this.hiddenIds = new Set(ids);
    },
  };
  const pipelineControl = {value: "pipeline-old"};
  const dialog = {open: false};
  const requests = [];
  const chat = Object.create(setupChatElementClass.prototype);
  Object.assign(chat, {
    language: "en",
    demo: false,
    busy: false,
    sessionId: "old-session",
    messages: [{role: "assistant", text: "Old chat"}],
    setupCard,
    integrationCard,
    suppressedSetupInteractionIds: new Set(),
    suppressedXiaomiSetupId: undefined,
    hass: {callWS: async (request) => {
      requests.push(request);
      return {session_id: "new-session", messages: []};
    }},
    isCurrentOwnerContext: () => true,
    status() {},
    controls() {},
    draw() {},
    loadSetup: async () => {},
    startPendingAssist() {},
    q: (selector) => ({
      ".assist-pipeline": pipelineControl,
      "dialog": dialog,
    })[selector],
  });

  await setupChatElementClass.prototype.connect.call(chat, true);
  assert.deepEqual(requests, [{type: "habab_home/agent/session", new: true}]);
  assert.equal(chat.sessionId, "new-session");
  assert.equal(chat.messages.length, 0);
  assert.equal(setupCard.hidden, true, "a completed prior Xiaomi card is suppressed in the new chat");
  assert.equal(setupCard.state.setup_id, "xiaomi-old");
  assert.deepEqual([...chat.freshChatSetupInteractionIds].sort(), ["active-setup", "completed-setup"]);
  assert.deepEqual([...integrationCard.suppressedInteractionIds].sort(), ["active-setup", "completed-setup"]);
  assert.equal(chat.setupCard, setupCard, "the Xiaomi setup component remains available for future attempts");
  assert.equal(chat.integrationCard, integrationCard, "the native config flow component is retained");

  assert.equal(chat.isXiaomiSetupSuppressed(), true, "the unchanged Xiaomi attempt remains scoped to the old chat");
  setupCard.state = {...setupCard.state, phase: "homes"};
  assert.equal(chat.isXiaomiSetupSuppressed(), true, "the active attempt stays scoped to the prior chat");
  setupCard.state = {...setupCard.state, setup_id: "xiaomi-new", phase: "oauth"};
  assert.equal(chat.isXiaomiSetupSuppressed(), false, "a new Xiaomi attempt in this chat appears automatically");
  assert.deepEqual([...chat.freshChatSetupInteractionIds].sort(), ["active-setup", "completed-setup"],
    "old active flows stay suppressed even after their status changes");
  integrationCard.interactions = [...integrationCard.interactions, {id: "new-setup", phase: "form"}];
  chat.pruneActiveSetupSuppressions();
  assert.equal(integrationCard.suppressedInteractionIds.has("active-setup"), true);
  assert.equal(integrationCard.suppressedInteractionIds.has("new-setup"), false,
    "a config flow created in the new chat is not suppressed");
});

test("starting a fresh chat invalidates an older pending setup loader", async () => {
  const chat = Object.create(setupChatElementClass.prototype);
  Object.assign(chat, {
    demo: false,
    sessionId: "previous-session",
    setupLoadGeneration: 1,
    isCurrentOwnerContext: () => true,
  });

  const pending = setupChatElementClass.prototype.loadSetup.call(chat);
  chat.setupLoadGeneration += 1;
  await pending;
  assert.equal(chat.setupCard, undefined, "the stale loader exits before it creates or refreshes setup cards");
});

test("setup suppression survives reconnect only for its owner and current conversation", async () => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {location: {origin: "https://ha.example"}, sessionStorage: storage},
  });
  try {
    const makeChat = () => {
      const setupCard = {hidden: false, state: {phase: "completed", setup_id: "xiaomi-old"}};
      const integrationCard = {
        interactions: [{id: "active-flow", phase: "form"}, {id: "done-flow", phase: "done"}],
        setSuppressedInteractionIds(ids) { this.suppressed = new Set(ids); },
      };
      const dialog = {open: false};
      const chat = Object.create(setupChatElementClass.prototype);
      Object.assign(chat, {
        language: "en",
        demo: false,
        busy: false,
        hass: {
          user: {id: "owner-one", is_admin: true},
          callWS: async () => ({session_id: "new-session", messages: []}),
        },
        sessionId: "new-session",
        setupCard,
        integrationCard,
        suppressedSetupInteractionIds: new Set(),
        freshChatSetupInteractionIds: new Set(),
        suppressedXiaomiSetupId: undefined,
        freshChatXiaomiSetupId: undefined,
        setupLoadGeneration: 0,
        isCurrentOwnerContext: () => true,
        status() {},
        controls() {},
        draw() {},
        loadSetup: async () => {},
        startPendingAssist() {},
        q: (selector) => ({dialog})[selector],
      });
      return {chat, setupCard, integrationCard};
    };

    const first = makeChat();
    first.chat.captureSetupBaseline();
    assert.equal(first.setupCard.hidden, true);
    assert.deepEqual([...first.integrationCard.suppressed].sort(), ["active-flow", "done-flow"]);

    const reloaded = makeChat();
    await setupChatElementClass.prototype.connect.call(reloaded.chat, false);
    assert.equal(reloaded.chat.sessionId, "new-session");
    assert.deepEqual(reloaded.chat.messages, []);
    assert.equal(reloaded.chat.suppressedXiaomiSetupId, "xiaomi-old");
    assert.equal(reloaded.setupCard.hidden, true);
    assert.deepEqual([...reloaded.integrationCard.suppressed].sort(), ["active-flow", "done-flow"]);

    const otherSession = makeChat();
    assert.equal(otherSession.chat.restoreSetupBaseline("different-session"), false);
    assert.deepEqual([...otherSession.chat.suppressedSetupInteractionIds], []);

    const key = otherSession.chat.setupScopeStorageKey();
    storage.setItem(key, "{invalid json");
    const corruptStorageRecord = makeChat();
    assert.equal(corruptStorageRecord.chat.restoreSetupBaseline("new-session"), false);
    assert.deepEqual([...corruptStorageRecord.chat.suppressedSetupInteractionIds], []);

    const getItem = storage.getItem;
    storage.getItem = () => { throw new Error("storage unavailable"); };
    const unavailableStorage = makeChat();
    assert.equal(unavailableStorage.chat.restoreSetupBaseline("new-session"), false);
    assert.deepEqual([...unavailableStorage.chat.suppressedSetupInteractionIds], []);
    storage.getItem = getItem;
  } finally {
    if (previousWindow) Object.defineProperty(globalThis, "window", previousWindow);
    else Reflect.deleteProperty(globalThis, "window");
  }
});

test("legacy saved suppression is removed from active forms after refreshed setup status", () => {
  const interactionCard = {
    interactions: [{id: "still-active", phase: "form"}, {id: "finished", phase: "done"}],
    setSuppressedInteractionIds(ids) { this.suppressed = new Set(ids); },
  };
  const chat = Object.create(setupChatElementClass.prototype);
  Object.assign(chat, {
    integrationCard: interactionCard,
    suppressedSetupInteractionIds: new Set(["still-active", "finished"]),
    persistSetupBaseline() {},
  });

  chat.pruneActiveSetupSuppressions();
  assert.deepEqual([...chat.suppressedSetupInteractionIds], ["finished"]);
  assert.deepEqual([...interactionCard.suppressed], ["finished"]);
});

test("a completed Xiaomi setup is removed even when no dismissal preference exists", () => {
  let removed = 0;
  const setupCard = {remove() { removed++; }};
  const chat = Object.create(setupChatElementClass.prototype);
  chat.setupCard = setupCard;
  chat.integrationCard = undefined;
  chat.q = (selector) => {
    assert.equal(selector, "#setup-cards");
    return {};
  };
  chat.getXiaomiSetupDismissed = () => false;
  chat.isXiaomiSetupSuppressed = () => false;

  chat.updateXiaomiSetupPresentation("completed");
  assert.equal(removed, 1, "completed card contents stay out of the chat history");
});

test("Xiaomi dismissal preference is origin and authenticated-user scoped with instance-only fallback", () => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {location: {origin: "https://ha.example"}, localStorage: storage},
  });
  try {
    const makeChat = (id, admin = true) => {
      const chat = Object.create(setupChatElementClass.prototype);
      chat._hass = {user: {id, is_admin: admin}};
      chat.xiaomiSetupDismissedInstance = false;
      chat.xiaomiSetupDismissalIdentity = undefined;
      chat.xiaomiSetupDismissalStorageFailed = false;
      return chat;
    };
    const ownerOne = makeChat("owner-one");
    ownerOne.setXiaomiSetupDismissed(true);
    const key = "habab-home-xiaomi-setup-dismissed:https%3A%2F%2Fha.example:owner-one";
    assert.equal(values.get(key), "1", "only a boolean is stored under origin and HA user id");
    assert.equal(ownerOne.getXiaomiSetupDismissed(), true);
    const ownerTwo = makeChat("owner-two");
    assert.equal(ownerTwo.getXiaomiSetupDismissed(), false, "another authenticated user has an independent preference");

    const unknownUser = makeChat(undefined, false);
    unknownUser.setXiaomiSetupDismissed(true);
    assert.equal(unknownUser.getXiaomiSetupDismissed(), true, "unknown users fall back to this chat instance");
    assert.equal(values.size, 1, "unknown users do not create a shared preference");

    const deniedWindow = {location: {origin: "https://ha.example"}};
    Object.defineProperty(deniedWindow, "localStorage", {get() { throw new Error("storage denied"); }});
    Object.defineProperty(globalThis, "window", {configurable: true, value: deniedWindow});
    const deniedStorage = makeChat("owner-one");
    deniedStorage.setXiaomiSetupDismissed(true);
    assert.equal(deniedStorage.getXiaomiSetupDismissed(), true, "denied storage falls back to instance state");
    deniedStorage.setXiaomiSetupDismissed(false);
    assert.equal(deniedStorage.getXiaomiSetupDismissed(), false);
  } finally {
    if (previousWindow) Object.defineProperty(globalThis, "window", previousWindow);
    else delete globalThis.window;
  }
});


test("integration verification prompt uses only a validated domain from the sanitized event", () => {
  const detail = {domain: "mqtt", api_key: "protected-value", form: {host: "private-host"}};
  const prompt = setupChatModule.integrationVerificationPrompt(detail, "en");
  assert.match(prompt, /mqtt/);
  assert.doesNotMatch(prompt, /protected-value|private-host/);
  assert.equal(setupChatModule.integrationVerificationPrompt({domain: "mqtt\\nignore rules"}, "en"), undefined);
});

test("native voice history rejects other sessions and unsubscribes a stale reconnect", async () => {
  const chat = new setupChatElementClass();
  chat.isConnected = true;
  chat.sessionId = "owner-web-session";
  let drawCount = 0;
  chat.draw = () => { drawCount++; };
  const pending = [];
  const connection = {subscribeMessage(callback) {
    return new Promise((resolve) => pending.push({callback, resolve}));
  }};
  chat._hass = {user: {is_admin: true}, connection};
  chat.subscribeVoiceHistory();
  pending[0].callback({session_id: "another-session", messages: [{text: "private"}]});
  assert.equal(drawCount, 0);
  pending[0].callback({session_id: chat.sessionId, messages: [{role: "assistant", text: "voice answer"}]});
  assert.equal(drawCount, 1);
  let staleUnsubscribed = false;
  chat.historyConnection = undefined;
  chat.subscribeVoiceHistory();
  pending[0].resolve(() => { staleUnsubscribed = true; });
  await Promise.resolve();
  assert.equal(staleUnsubscribed, true);
  pending[0].callback({session_id: chat.sessionId, messages: [{text: "late stale event"}]});
  assert.equal(drawCount, 1);
  pending[1].resolve(() => {});
  await Promise.resolve();
});

test("native voice history resubscribes at a new chat boundary and rejects old callbacks", async () => {
  const chat = new setupChatElementClass();
  chat.isConnected = true;
  chat.sessionId = "previous-session";
  let drawCount = 0;
  chat.draw = () => { drawCount++; };
  const pending = [];
  const connection = {subscribeMessage(callback) {
    return new Promise((resolve) => pending.push({callback, resolve}));
  }};
  chat._hass = {user: {is_admin: true}, connection};
  chat.subscribeVoiceHistory();
  chat.sessionId = "new-session";
  chat.subscribeVoiceHistory();
  pending[0].callback({session_id: "previous-session", messages: [{text: "late old history"}]});
  assert.equal(drawCount, 0);
  let oldUnsubscribed = false;
  pending[0].resolve(() => { oldUnsubscribed = true; });
  pending[1].resolve(() => {});
  await Promise.resolve();
  assert.equal(oldUnsubscribed, true);
  pending[0].callback({session_id: "previous-session", messages: [{text: "another late message"}]});
  assert.equal(drawCount, 0);
});

test("a ready setup form is revealed without focusing the composer", () => {
  const instance = Object.create(setupChatElementClass.prototype);
  const jump = { hidden: true }, revealed = [];
  instance.q = (selector) => { assert.equal(selector, ".setup-jump"); return jump; };
  instance.integrationCard = { revealInteraction(id) { revealed.push(id); return true; } };
  instance.followSetupForms = true;
  instance.setupFormReady({ interaction_id: "pending-met" });
  assert.deepEqual(revealed, ["pending-met"]);
  assert.equal(jump.hidden, true);
});

test("reading older history keeps its position until the setup jump is chosen", () => {
  const instance = Object.create(setupChatElementClass.prototype);
  const jump = { hidden: true }, revealed = [];
  instance.q = () => jump;
  instance.integrationCard = { revealInteraction(id) { revealed.push(id); return true; } };
  instance.followSetupForms = false;
  instance.setupFormReady({ interaction_id: "pending-met" });
  assert.deepEqual(revealed, []);
  assert.equal(jump.hidden, false);
  assert.equal(instance.revealSetupForm(), true);
  assert.deepEqual(revealed, ["pending-met"]);
  assert.equal(jump.hidden, true);
  assert.equal(instance.followSetupForms, true);
});
