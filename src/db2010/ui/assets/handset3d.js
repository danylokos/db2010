import * as THREE from './vendor/three.module.js';
import {GLTFLoader} from './vendor/GLTFLoader.js';
import {OrbitControls} from './vendor/OrbitControls.js';

class Handset3D {
  constructor({host, lcd, contact, focus, failure, viewChanged, tooltip = () => ''}) {
    this.host = host;
    this.contact = contact;
    this.focusHandset = focus;
    this.failure = failure;
    this.tooltip = tooltip;
    this.active = false;
    this.enabled = false;
    this.disposed = false;
    this.keys = new Map();
    this.keyPointers = new Map();
    this.orbitPointers = new Set();
    this.pressed = new Set();
    this.focused = null;
    this.hovered = null;
    this.frame = null;
    this.renders = 0;
    this.waiting = true;
    this.reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
    this.renderer = new THREE.WebGLRenderer({alpha: true, antialias: true});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, .01, 20);
    this.camera.position.set(0, 0, 1.9);
    const hemisphere = new THREE.HemisphereLight(0xf6f8ff, 0x4d4137, 1.2);
    this.scene.add(hemisphere);
    const key = new THREE.DirectionalLight(0xffffff, 2.2);
    key.position.set(-2, 3, 4);
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xffeee0, 1.1);
    fill.position.set(3, 0, -2);
    this.scene.add(fill);
    this.texture = new THREE.CanvasTexture(lcd);
    this.texture.flipY = false;
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.texture.minFilter = this.texture.magFilter = THREE.NearestFilter;
    this.texture.generateMipmaps = false;
    this.screenMaterial = new THREE.MeshBasicMaterial({map: this.texture, toneMapped: false});
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.canvas = this.renderer.domElement;
    this.canvas.setAttribute('aria-hidden', 'true');
    this.listeners = [];
    const listen = (target, type, callback, options) => {
      target.addEventListener(type, callback, options);
      this.listeners.push(() => target.removeEventListener(type, callback, options));
    };
    // Capture before OrbitControls so a physical button owns its complete
    // gesture; dragging across a key never starts a new contact.
    listen(this.canvas, 'pointerdown', event => this.pointerDown(event), true);
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      listen(this.canvas, type, event => this.pointerEnd(event), true);
    }
    listen(this.canvas, 'pointermove', event => {
      if (this.keyPointers.size || this.orbitPointers.size) return;
      this.hovered = this.enabled && event.pointerType === 'mouse' ? this.pick(event) : null;
      this.feedback();
    });
    listen(this.canvas, 'pointerleave', () => { this.hovered = null; this.feedback(); });
    listen(this.canvas, 'webglcontextlost', event => {
      event.preventDefault();
      this.failure(new Error('3D graphics context lost'));
    });
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enablePan = false;
    this.controls.enableDamping = false;
    this.controls.minPolarAngle = .05;
    this.controls.maxPolarAngle = Math.PI-.05;
    // Do not attach camera keyboard listeners: arrows belong to the phone.
    this.controls.addEventListener('change', () => {
      this.frameZoom();
      const rotated = Math.abs(this.controls.getAzimuthalAngle()) > .0001 ||
        Math.abs(this.controls.getPolarAngle()-Math.PI/2) > .0001;
      viewChanged(rotated);
      this.hovered = null;
      this.feedback();
    });
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(host);
    host.append(this.canvas);
  }

  async load() {
    const abort = new AbortController();
    this.abort = abort;
    const deadline = setTimeout(() => abort.abort(), 30000);
    try {
      const response = await fetch('/model/w800i.glb', {signal: abort.signal});
      if (!response.ok) throw new Error('Model request failed ('+response.status+')');
      const gltf = await new GLTFLoader().parseAsync(await response.arrayBuffer(), location.origin+'/model/');
      if (this.disposed) {
        this.disposeObject(gltf.scene);
        throw new Error('3D view closed');
      }
      this.model = gltf.scene;
      this.scene.add(this.model);
      this.model.traverse(mesh => {
        if (!mesh.isMesh) return;
        if (mesh.name === 'lcd') {
          mesh.material = this.screenMaterial;
          this.screen = mesh;
        } else if (mesh.name.startsWith('key-')) {
          mesh.material = mesh.material.clone();
          this.keys.set(mesh.name.slice(4), mesh);
        } else if (mesh.name === 'joystick') {
          this.joystick = mesh;
          this.joystickBounds = new THREE.Box3().setFromObject(mesh);
          this.joystickCenter = this.joystickBounds.getCenter(new THREE.Vector3());
        }
      });
      this.addRearReflections();
      if (!this.screen || !this.joystick || this.keys.size !== 22) throw new Error('Incomplete handset geometry');
      const bounds = new THREE.Box3().setFromObject(this.model);
      this.modelCorners = [];
      for (const x of [bounds.min.x, bounds.max.x]) {
        for (const y of [bounds.min.y, bounds.max.y]) {
          for (const z of [bounds.min.z, bounds.max.z]) this.modelCorners.push(new THREE.Vector3(x,y,z));
        }
      }
      this.screenCenter = new THREE.Box3().setFromObject(this.screen).getCenter(new THREE.Vector3());
      this.addActivityIndicator();
      this.addJoystickHighlights();
      this.feedback();
      this.resize();
    } finally {
      clearTimeout(deadline);
      this.abort = null;
    }
  }

  addRearReflections() {
    // Neutral studio cards give the selfie mirror and frosted flash reflected
    // highlights without changing the casing or other controls' lighting.
    const canvas = document.createElement('canvas');
    canvas.width = 256; canvas.height = 128;
    const context = canvas.getContext('2d');
    const gradient = context.createLinearGradient(0, 0, 0, 128);
    for (const [position, color] of [[0, '#c8ced4'], [.45, '#626970'], [.55, '#30353a'], [1, '#9ca2a8']]) {
      gradient.addColorStop(position, color);
    }
    context.fillStyle = gradient; context.fillRect(0, 0, 256, 128);
    context.fillStyle = '#f6f8fa'; context.fillRect(20, 16, 35, 92);
    context.fillStyle = '#d8dde2'; context.fillRect(155, 28, 55, 72);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.mapping = THREE.EquirectangularReflectionMapping;
    const generator = new THREE.PMREMGenerator(this.renderer);
    this.rearEnvironment = generator.fromEquirectangular(texture);
    generator.dispose(); texture.dispose();
    this.model.traverse(mesh => {
      if (mesh.isMesh && (mesh.material.name.startsWith('Rear chrome ') ||
                          mesh.material.name === 'Frosted LED flash glass')) {
        mesh.material.envMap = this.rearEnvironment.texture;
      }
    });
  }

  addActivityIndicator() {
    const bounds = new THREE.Box3().setFromObject(this.screen);
    this.activity = new THREE.Group();
    this.activity.position.copy(bounds.getCenter(new THREE.Vector3()));
    this.activity.position.z = bounds.max.z+.0003;
    const material = color => new THREE.MeshBasicMaterial({color, toneMapped: false});
    this.activity.add(new THREE.Mesh(new THREE.RingGeometry(.017, .022, 48), material(0x394149)));
    this.spinner = new THREE.Mesh(new THREE.RingGeometry(.017, .022, 36, 1, 0, Math.PI*1.5), material(0xd4dce3));
    this.spinner.position.z = .0001;
    this.activity.add(this.spinner);
    this.activity.visible = this.waiting;
    this.scene.add(this.activity);
  }

  addJoystickHighlights() {
    this.joystickHighlights = new Map();
    for (const [action, start] of [['right', -Math.PI/4], ['up', Math.PI/4],
                                 ['left', 3*Math.PI/4], ['down', 5*Math.PI/4], ['select', null]]) {
      const geometry = start === null ? new THREE.CircleGeometry(.012, 24) :
        new THREE.RingGeometry(.014, .035, 24, 1, start, Math.PI/2);
      const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({color: 0xffa126,
        transparent: true, opacity: .7, depthWrite: false, toneMapped: false}));
      mesh.position.copy(this.joystickCenter);
      mesh.position.z = this.joystickBounds.max.z+.0004;
      mesh.visible = false;
      mesh.raycast = () => {};
      this.scene.add(mesh);
      this.joystickHighlights.set(action, mesh);
    }
  }

  resize() {
    if (this.disposed) return;
    const {width, height} = this.host.getBoundingClientRect();
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width/height;
    this.camera.updateProjectionMatrix();
    const fit = Math.max(1, .48/this.camera.aspect)*.58/Math.tan(THREE.MathUtils.degToRad(17.5))+.1;
    if (this.fit) {
      this.camera.position.sub(this.controls.target).multiplyScalar(fit/this.fit).add(this.controls.target);
    } else {
      this.camera.position.set(0, 0, fit);
    }
    this.fit = fit;
    this.controls.minDistance = fit*.45;
    this.controls.maxDistance = fit*3;
    this.controls.update();
    this.frameZoom();
    this.hovered = null;
    this.feedback();
  }

  frameZoom() {
    if (!this.modelCorners || !this.fit) return;
    const inverse = this.camera.quaternion.clone().invert();
    const tanY = Math.tan(THREE.MathUtils.degToRad(this.camera.fov/2));
    const tanX = tanY*this.camera.aspect;
    // Fit the original bounds about the origin in the current view. Using
    // already shifted bounds here would make the framing feed back on itself.
    let fullDistance = 0;
    for (const corner of this.modelCorners) {
      const p = corner.clone().applyQuaternion(inverse);
      fullDistance = Math.max(fullDistance, p.z+Math.max(Math.abs(p.x)/tanX,
        Math.abs(p.y)/tanY, this.camera.near));
    }
    const min = this.controls.minDistance;
    const t = fullDistance > min ? THREE.MathUtils.clamp(
      (fullDistance-this.controls.getDistance())/(fullDistance-min), 0, 1) : 0;
    const target = this.screenCenter.clone().multiplyScalar(t*t*(3-2*t));
    // Translate both ends of the viewing ray: preserve orbit angle/distance,
    // with no recursive controls.update() or animation after the gesture.
    this.camera.position.add(target.clone().sub(this.controls.target));
    this.controls.target.copy(target);
    this.camera.updateMatrixWorld();
  }

  front() {
    if (!this.fit || this.disposed) return;
    this.controls.target.set(0, 0, 0);
    this.camera.position.set(0, 0, this.fit);
    this.camera.up.set(0, 1, 0);
    this.controls.update();
    this.invalidate();
  }

  focusContact(action) {
    this.front();
    const direction = ({'volume-up': [1,0,.3], 'volume-down': [1,0,.3],
      camera: [1,0,.3], 'camera-focus': [1,0,.3],
      'play-pause': [-1,0,.3], power: [0,1,.3]})[action];
    if (!direction || !this.fit || this.disposed) return;
    this.camera.position.fromArray(direction).normalize().multiplyScalar(this.fit);
    this.controls.update();
    this.invalidate();
  }

  pick(event) {
    if (!this.model) return null;
    const rect = this.canvas.getBoundingClientRect();
    this.pointer.set((event.clientX-rect.left)/rect.width*2-1, -(event.clientY-rect.top)/rect.height*2+1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    // Include the casing in the closest-hit test so rear/occluded keys cannot
    // be pressed through the body.
    const hit = this.raycaster.intersectObject(this.model, true)[0];
    if (!hit) return null;
    if (hit.object.name.startsWith('key-')) {
      const action = hit.object.name.slice(4);
      return action === 'camera' && event.shiftKey ? 'camera-focus' : action;
    }
    if (hit.object === this.joystick || hit.object.name === 'joystick-tip') {
      const dx = hit.point.x-this.joystickCenter.x, dy = hit.point.y-this.joystickCenter.y;
      if (Math.hypot(dx, dy) < .013) return 'select';
      return Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'up' : 'down');
    }
    return null;
  }

  pointerDown(event) {
    if (event.button !== 0) return;
    this.hovered = null;
    this.feedback();
    this.focusHandset();
    const action = this.enabled && !this.orbitPointers.size ? this.pick(event) : null;
    if (action || this.keyPointers.size) {
      event.stopImmediatePropagation();
      event.preventDefault();
      if (!action) return;
      this.controls.enabled = false;
      this.keyPointers.set(event.pointerId, action);
      this.canvas.setPointerCapture(event.pointerId);
      this.contact('pointer:'+event.pointerId, action, true);
    } else {
      this.orbitPointers.add(event.pointerId);
    }
  }

  pointerEnd(event) {
    this.orbitPointers.delete(event.pointerId);
    if (!this.keyPointers.has(event.pointerId)) return;
    event.stopImmediatePropagation();
    event.preventDefault();
    this.keyPointers.delete(event.pointerId);
    this.contact('pointer:'+event.pointerId, null, false);
    if (this.canvas.hasPointerCapture(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId);
    this.controls.enabled = this.keyPointers.size === 0;
  }

  release() {
    for (const id of Array.from(this.keyPointers.keys())) {
      this.keyPointers.delete(id);
      this.contact('pointer:'+id, null, false);
      if (this.canvas.hasPointerCapture(id)) this.canvas.releasePointerCapture(id);
    }
    for (const id of Array.from(this.orbitPointers)) {
      this.canvas.dispatchEvent(new PointerEvent('pointercancel', {pointerId: id}));
    }
    this.orbitPointers.clear();
    this.controls.enabled = true;
    this.hovered = null;
    this.feedback();
  }

  contacts(pressed, focused = null) {
    this.pressed = new Set(pressed);
    this.focused = focused;
    this.feedback();
  }

  feedback() {
    for (const [action, mesh] of this.keys) {
      const aliases = action === 'camera' ? ['camera', 'camera-focus'] : [action];
      const strength = aliases.some(key => this.pressed.has(key)) ? .85 :
        (aliases.includes(this.focused) ? .4 : (aliases.includes(this.hovered) ? .16 : 0));
      mesh.material.emissive.setRGB(strength, strength*.3, strength*.015);
    }
    for (const [action, mesh] of this.joystickHighlights || []) {
      mesh.visible = this.pressed.has(action) || action === this.focused || action === this.hovered;
    }
    this.canvas.style.cursor = this.hovered ? 'pointer' : 'grab';
    const title = this.enabled && this.hovered && !this.keyPointers.size && !this.orbitPointers.size ?
      this.tooltip(this.hovered) : '';
    if (this.canvas.title !== title) this.canvas.title = title;
    this.invalidate();
  }

  display(waiting = this.waiting) {
    this.waiting = waiting;
    if (this.activity) this.activity.visible = waiting;
    this.texture.needsUpdate = true;
    this.invalidate();
  }

  setActive(active) {
    this.active = active;
    if (!active) {
      this.release();
      if (this.frame !== null) cancelAnimationFrame(this.frame);
      this.frame = null;
    } else {
      this.resize();
      this.display();
    }
  }

  invalidate() {
    if (this.disposed || !this.active || document.hidden || this.frame !== null) return;
    this.frame = requestAnimationFrame(time => {
      this.frame = null;
      if (!this.active || this.disposed || document.hidden) return;
      if (this.waiting && this.spinner) this.spinner.rotation.z = -time*.006;
      this.renderer.render(this.scene, this.camera);
      this.renders++;
      if (this.waiting && !this.reducedMotion.matches) this.invalidate();
    });
  }

  disposeObject(object) {
    const textures = new Set(), materials = new Set();
    object.traverse(mesh => {
      mesh.geometry?.dispose();
      for (const material of (Array.isArray(mesh.material) ? mesh.material : [mesh.material])) {
        if (!material) continue;
        materials.add(material);
        for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
      }
    });
    for (const texture of textures) { texture.source?.data?.close?.(); texture.dispose(); }
    for (const material of materials) material.dispose();
  }

  dispose() {
    if (this.disposed) return;
    this.setActive(false);
    this.disposed = true;
    this.abort?.abort();
    this.observer.disconnect();
    for (const remove of this.listeners) remove();
    this.controls.dispose();
    this.disposeObject(this.scene);
    this.rearEnvironment?.dispose();
    this.texture.dispose();
    this.screenMaterial.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.canvas.remove();
  }
}

export async function createHandset(options) {
  const viewer = new Handset3D(options);
  try {
    await viewer.load();
    return viewer;
  } catch (error) {
    viewer.dispose();
    throw error;
  }
}
