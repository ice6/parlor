// The avatar: a face that opens its mouth when Parlor speaks.
//
// It reads ttsAnalyser (app.js) every frame and drives a viseme from the
// spectrum. That is the whole design: the analyser IS the playback clock,
// so lip-sync is exact by construction — nothing is scheduled, buffered,
// or predicted, and there is no way for the mouth to drift. A remote
// renderer (a GPU box on the LAN painting a real face) would instead live
// off onAudioChunk's startAt; see the seam contract in app.js.
//
// Two backends, one interface. CanvasFaceBackend has no dependencies at
// all and is the automatic fallback when the VRM asset or the CDN is
// missing — a black rectangle where the assistant's face should be is
// worse than a simple drawn one.
//
// Frame budget is not a nicety: @ricky0123/vad-web runs Silero through
// WASM on this same main thread, and a greedy render loop starves turn
// detection and barge-in — the two things Parlor is actually good at.
// Hence 30fps, no rendering while the tab is hidden, and a pixel ratio
// capped well below a retina display's.

const FPS = 30;
const FRAME_MS = 1000 / FPS;

// Band edges in Hz, resolved to bin indices against the live sample rate —
// never hardcoded, since Firefox and Safari run contexts at 44.1kHz.
const BANDS = {
  lo: [250, 900],     // F1 proxy — jaw aperture: open vowels (aa, oh)
  hi: [900, 2600],    // F2 proxy — tongue frontness: front vowels (ee, ih)
  sib: [4000, 8000],  // fricatives /s ʃ f/ — mouth nearly closed, no vowel
};

// Vowel anchors on the ratio axis. Targets blend linearly between the two
// bracketing anchors, so the mouth never snaps between shapes.
const VOWELS = [['ou', 0.10], ['oh', 0.33], ['aa', 0.50], ['ih', 0.68], ['ee', 0.90]];

// Asymmetric: a mouth opens faster than it closes, and smoothing both
// edges at the same rate reads as mush.
const ATTACK = 0.45, RELEASE = 0.18;

const SILENCE_LEVEL = 0.04;   // below this, for…
const SILENCE_HOLD_MS = 120;  // …this long → close the mouth between sentences

/** Shared mouth model: spectrum in, viseme weights out. */
class Mouth {
  constructor(analyser) {
    this.analyser = analyser;
    this.bins = new Uint8Array(analyser.frequencyBinCount);  // allocated ONCE
    const hzPerBin = analyser.context.sampleRate / analyser.fftSize;
    this.ranges = {};
    for (const [name, [from, to]] of Object.entries(BANDS)) {
      this.ranges[name] = [
        Math.max(1, Math.floor(from / hzPerBin)),
        Math.min(this.bins.length, Math.ceil(to / hzPerBin)),
      ];
    }
    this.peak = 0.08;
    this.quietSince = 0;
    this.weights = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
    this.open = 0;
    this.ratio = 0.5;
  }

  band(name) {
    const [from, to] = this.ranges[name];
    let sum = 0;
    for (let i = from; i < to; i++) sum += this.bins[i];
    return to > from ? sum / (to - from) / 255 : 0;
  }

  /** Advance one frame. `speaking` false forces the mouth shut. */
  update(now, speaking) {
    let targets = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
    if (speaking) {
      this.analyser.getByteFrequencyData(this.bins);
      const lo = this.band('lo'), hi = this.band('hi'), sib = this.band('sib');
      const raw = Math.sqrt(lo * lo + hi * hi);
      // Adaptive normaliser: decays slowly so a quiet passage still
      // animates instead of flatlining under a loud sentence's peak.
      this.peak = Math.max(this.peak * 0.995, raw);
      const level = Math.min(1, raw / Math.max(this.peak, 0.08));
      const sibilance = sib / (lo + hi + sib + 1e-6);

      if (level < SILENCE_LEVEL) {
        if (!this.quietSince) this.quietSince = now;
      } else {
        this.quietSince = 0;
      }
      const quiet = this.quietSince && now - this.quietSince > SILENCE_HOLD_MS;

      if (!quiet) {
        this.ratio = hi / (lo + hi + 1e-6);
        const open = level * (1 - 0.75 * sibilance);
        let i = 0;
        while (i < VOWELS.length - 2 && this.ratio > VOWELS[i + 1][1]) i++;
        const [aName, a] = VOWELS[i], [bName, b] = VOWELS[i + 1];
        const f = Math.max(0, Math.min(1, (this.ratio - a) / (b - a)));
        targets[aName] = open * (1 - f);
        targets[bName] = open * f;
        // A sibilant with no vowel would leave a dead face; show a hint of
        // a tooth-line instead.
        targets.ih = Math.max(targets.ih, 0.18 * sibilance * level);
      }
    }

    let open = 0;
    for (const k of Object.keys(this.weights)) {
      const t = targets[k];
      this.weights[k] += (t - this.weights[k]) * (t > this.weights[k] ? ATTACK : RELEASE);
      open = Math.max(open, this.weights[k]);
    }
    this.open = open;
    return this.weights;
  }
}

/** Blink timing + idle head drift, shared by both backends. */
class Idle {
  constructor() {
    this.blink = 0;
    this.nextBlink = performance.now() / 1000 + 2;
    this.blinkStart = 0;
    this.encore = false;
  }

  update(t) {
    // People blink while they talk, so this is never suppressed.
    if (!this.blinkStart && t >= this.nextBlink) {
      this.blinkStart = t;
    }
    if (this.blinkStart) {
      const p = (t - this.blinkStart) / 0.12;
      if (p >= 1) {
        this.blink = 0;
        this.blinkStart = 0;
        if (!this.encore && Math.random() < 0.15) {
          this.encore = true;
          this.nextBlink = t + 0.18;
        } else {
          this.encore = false;
          this.nextBlink = t + 2.4 + Math.random() * 3.8;
        }
      } else {
        this.blink = Math.sin(Math.PI * p);
      }
    }
    return this.blink;
  }

  // Two summed sines at unrelated frequencies, so the loop never becomes
  // perceptible the way a single sine does.
  sway(t, amp) {
    return {
      yaw: amp * (0.050 * Math.sin(t * 0.31) + 0.020 * Math.sin(t * 0.73)),
      pitch: amp * (0.030 * Math.sin(t * 0.23) + 0.015 * Math.sin(t * 0.61)),
      roll: amp * (0.018 * Math.sin(t * 0.17)),
    };
  }
}

// Per-state posture. The avatar's only input from the app besides audio.
const POSTURE = {
  listening: { amp: 1.0, yaw: 0, pitch: 0, expression: ['relaxed', 0.10] },
  processing: { amp: 0.7, yaw: 0.05, pitch: -0.06, expression: null },
  speaking: { amp: 1.35, yaw: 0, pitch: 0, expression: ['happy', 0.15] },
  loading: { amp: 0.4, yaw: 0, pitch: 0.08, expression: null },
};

/** Base class: owns the frame loop, the mouth, and the idle animation. */
class Backend {
  constructor(canvas) {
    this.canvas = canvas;
    this.speaking = false;
    this.state = 'loading';
    this.bias = { yaw: 0, pitch: 0, amp: 1 };
    this.idle = new Idle();
    this.raf = null;
    this.lastFrame = 0;
  }

  init({ ttsAnalyser }) {
    this.mouth = new Mouth(ttsAnalyser);
    this.resize();
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(this.canvas);
    this.loop = this.loop.bind(this);
    this.raf = requestAnimationFrame(this.loop);
  }

  setState(state) {
    this.state = state;
    this.speaking = state === 'speaking';
  }

  onAudioChunk() { /* the analyser is the clock — nothing to schedule */ }

  stop() { this.speaking = false; }

  dispose() {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.observer?.disconnect();
    this.raf = null;
  }

  loop(ms) {
    this.raf = requestAnimationFrame(this.loop);
    // A hidden tab must cost nothing: rAF usually throttles, but a
    // background window with a visible sliver does not.
    if (document.visibilityState !== 'visible') return;
    if (ms - this.lastFrame < FRAME_MS) return;
    const dt = Math.min(0.1, (ms - this.lastFrame) / 1000);
    this.lastFrame = ms;

    const t = ms / 1000;
    const posture = POSTURE[this.state] || POSTURE.loading;
    // Ease toward the new posture so a state change doesn't snap the head.
    for (const k of ['yaw', 'pitch', 'amp']) {
      this.bias[k] += ((posture[k] ?? 0) - this.bias[k]) * 0.06;
    }
    const weights = this.mouth.update(ms, this.speaking);
    const blink = this.idle.update(t);
    const sway = this.idle.sway(t, this.bias.amp);
    this.render({ weights, blink, sway, posture, dt, open: this.mouth.open,
                  ratio: this.mouth.ratio });
  }

  resize() {}
  render() {}
}

// ── 2D fallback face ──────────────────────────────────────────────────────
// No dependencies, no assets, ~zero GPU. It is the fallback when the VRM
// is missing, and a deliberate choice for anyone who'd rather not pay a
// 3D renderer's share of a machine that is already running an LLM and a
// TTS model on the same GPU.
export class CanvasFaceBackend extends Backend {
  init(ctx) {
    this.g = this.canvas.getContext('2d');
    super.init(ctx);
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    this.canvas.width = Math.max(1, rect.width * dpr);
    this.canvas.height = Math.max(1, rect.height * dpr);
    this.w = rect.width;
    this.h = rect.height;
    this.g.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  render({ blink, sway, open, ratio }) {
    const g = this.g, w = this.w, h = this.h;
    if (!w || !h) return;
    // Follow the app's state colour, so the face is part of the same
    // visual language as the glow and the waveform.
    const glow = getComputedStyle(document.documentElement)
      .getPropertyValue('--glow').trim() || '#818cf8';

    g.clearRect(0, 0, w, h);
    g.save();
    g.translate(w / 2 + sway.yaw * w * 0.5, h / 2 + sway.pitch * h * 0.6);
    g.rotate(sway.roll);

    const r = Math.min(w, h) * 0.30;
    g.strokeStyle = glow;
    g.fillStyle = glow;
    g.lineWidth = Math.max(1.5, r * 0.035);
    g.lineCap = 'round';
    g.lineJoin = 'round';

    // Head: a faint fill under the outline. A bare wireframe reads as a
    // diagram of a face rather than a face.
    g.globalAlpha = 0.07;
    g.beginPath();
    g.ellipse(0, 0, r * 0.86, r, 0, 0, Math.PI * 2);
    g.fill();
    g.globalAlpha = 0.24;
    g.stroke();

    // Eyes, each with a catchlight. The highlight is the single cheapest
    // thing that makes drawn eyes look alive instead of staring.
    const eyeY = -r * 0.20, eyeX = r * 0.34;
    const lid = Math.max(0.06, 1 - blink);
    for (const sx of [-1, 1]) {
      g.globalAlpha = 0.92;
      g.beginPath();
      g.ellipse(sx * eyeX, eyeY, r * 0.115, r * 0.145 * lid, 0, 0, Math.PI * 2);
      g.fill();
      if (lid > 0.5) {
        g.globalAlpha = 0.95;
        g.fillStyle = '#ffffff';
        g.beginPath();
        g.ellipse(sx * eyeX + r * 0.04, eyeY - r * 0.05 * lid,
                  r * 0.035, r * 0.035 * lid, 0, 0, Math.PI * 2);
        g.fill();
        g.fillStyle = glow;
      }
    }

    // Brows: gentle arches, highest in the middle. The first version drew
    // straight lines whose INNER ends sat lower than the outer ones —
    // that is the anger vector, and the face read as a scowl.
    g.globalAlpha = 0.7;
    g.lineWidth = Math.max(1.5, r * 0.030);
    for (const sx of [-1, 1]) {
      g.beginPath();
      g.moveTo(sx * (eyeX - r * 0.17), eyeY - r * 0.27);
      g.quadraticCurveTo(sx * eyeX, eyeY - r * 0.40,
                         sx * (eyeX + r * 0.18), eyeY - r * 0.28);
      g.stroke();
    }

    // The mouth is a SMILE that opens, not an ellipse: its corners always
    // sit above its centre, so a closed mouth is a curve rather than the
    // flat bar the first version drew. `open` deepens the lower lip;
    // `ratio` widens it toward front vowels (ee) and narrows it toward
    // rounded ones (ou), which is the way real mouths move.
    g.globalAlpha = 0.95;
    const my = r * 0.38;
    const lift = r * 0.09;                       // how far the corners rise
    const mh = r * open * 0.34;
    const mw = r * (0.26 + 0.12 * ratio);
    g.beginPath();
    g.moveTo(-mw, my - lift);
    g.quadraticCurveTo(0, my + lift * 1.9 + mh * 1.7, mw, my - lift);
    g.quadraticCurveTo(0, my + lift * 0.15 + mh * 0.25, -mw, my - lift);
    g.fill();

    // Cheeks: two soft dots that appear only on a wide smile. Small
    // touch, and the thing that finally reads as warm rather than polite.
    if (open > 0.25) {
      g.globalAlpha = 0.10 * Math.min(1, (open - 0.25) * 3);
      for (const sx of [-1, 1]) {
        g.beginPath();
        g.ellipse(sx * r * 0.52, r * 0.26, r * 0.13, r * 0.09, 0, 0, Math.PI * 2);
        g.fill();
      }
    }

    g.restore();
  }
}

// ── VRM 3D character ──────────────────────────────────────────────────────
class VrmAvatarBackend extends Backend {
  constructor(canvas, url) {
    super(canvas);
    this.url = url;
  }

  /** Load three + the model. Returns false if either is unavailable, so
   *  the caller can fall through to the 2D face. */
  async tryLoad() {
    try {
      const THREE = await import('three');
      const { GLTFLoader } = await import('three/addons/loaders/GLTFLoader.js');
      const { VRMLoaderPlugin, VRMUtils } = await import('@pixiv/three-vrm');
      this.THREE = THREE;

      const loader = new GLTFLoader();
      loader.register(parser => new VRMLoaderPlugin(parser));
      const gltf = await loader.loadAsync(this.url);
      const vrm = gltf.userData.vrm;
      if (!vrm) return false;

      // Unconditional: a no-op on VRM 1.0, and essential for the VRM 0.x
      // files most people have — those face +Z, i.e. away from the camera.
      VRMUtils.rotateVRM0(vrm);
      VRMUtils.removeUnnecessaryVertices(gltf.scene);
      VRMUtils.combineSkeletons(gltf.scene);
      this.vrm = vrm;
      this.gltf = gltf;
      return true;
    } catch (e) {
      console.warn('avatar: VRM unavailable —', e.message);
      return false;
    }
  }

  init(ctx) {
    const THREE = this.THREE;
    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas, alpha: true, antialias: true,
    });
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    // Capped well under a retina ratio: this is a talking head on a
    // machine already running an LLM on the same GPU.
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));

    this.scene = new THREE.Scene();
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.2);
    key.position.set(1, 1.6, 1.4);
    this.scene.add(key);

    // Half-body portrait framing, which is what fits a 16/9 stage.
    this.camera = new THREE.PerspectiveCamera(28, 16 / 9, 0.1, 20);
    this.camera.position.set(0, 1.36, 0.85);
    this.camera.lookAt(0, 1.32, 0);
    this.scene.add(this.camera);

    this.scene.add(this.gltf.scene);
    if (this.vrm.lookAt) {
      const target = new THREE.Object3D();
      target.position.set(0, 0, -1);
      this.camera.add(target);
      this.vrm.lookAt.target = target;
    }
    this.neck = this.vrm.humanoid?.getNormalizedBoneNode('neck');
    this.neckRest = this.neck
      ? { x: this.neck.rotation.x, y: this.neck.rotation.y, z: this.neck.rotation.z }
      : null;

    super.init(ctx);
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height || !this.renderer) return;
    this.camera.aspect = rect.width / rect.height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(rect.width, rect.height, false);
  }

  render({ weights, blink, sway, posture, dt }) {
    const em = this.vrm.expressionManager;
    if (em) {
      for (const [name, w] of Object.entries(weights)) em.setValue(name, w);
      em.setValue('blink', blink);
      for (const name of ['relaxed', 'happy']) {
        const active = posture.expression?.[0] === name;
        em.setValue(name, active ? posture.expression[1] : 0);
      }
    }
    if (this.neck) {
      this.neck.rotation.y = this.neckRest.y + sway.yaw + this.bias.yaw;
      this.neck.rotation.x = this.neckRest.x + sway.pitch + this.bias.pitch;
      this.neck.rotation.z = this.neckRest.z + sway.roll;
    }
    // vrm.update applies expression values, so it must come LAST — setting
    // them after it is a silent no-op.
    this.vrm.update(dt);
    this.renderer.render(this.scene, this.camera);
  }

  dispose() {
    super.dispose();
    this.renderer?.dispose();
  }
}

// ── Wiring ────────────────────────────────────────────────────────────────
// Everything is wrapped: a CDN outage, a missing model, or a WebGL-less
// browser must degrade to the pre-avatar UI (the glow still works, the
// canvas is just empty) — never a blank page.
try {
  const tag = document.querySelector('script[data-backend]');
  const wanted = tag?.dataset.backend || 'vrm';
  const canvas = document.getElementById('avatar');

  let backend = null;
  if (wanted === 'vrm' && canvas) {
    const vrm = new VrmAvatarBackend(canvas, tag.dataset.vrmUrl);
    // A missing .vrm (the file is gitignored — it's 10-60MB and the
    // likeness should be the user's choice) must not leave a black
    // rectangle where the assistant's face is.
    backend = await vrm.tryLoad() ? vrm : new CanvasFaceBackend(canvas);
  } else if (wanted === 'canvas' && canvas) {
    backend = new CanvasFaceBackend(canvas);
  } else if (canvas) {
    canvas.hidden = true;  // AVATAR_BACKEND=none — the pre-avatar layout
  }
  if (backend) window.setAvatarBackend(backend);
} catch (e) {
  console.warn('avatar disabled:', e);
}
