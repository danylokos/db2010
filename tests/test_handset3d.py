"""Real WebGL display, model picking, input ownership and independent loading."""
from support.guarded import run as guarded_run
import asyncio
import json

import pytest
from playwright.sync_api import sync_playwright, expect

from db2010.ui.browser import Browser
from db2010.ui.keypad import BUTTONS
from test_browser import panel


@pytest.fixture
def view():
    browser = Browser(0)
    browser.input.enable(True)
    browser.phase('run')
    browser.display(panel(bytes.fromhex('07e0')*(176*220)))
    browser.progress({'observations': 100000000, 'pc': 0x44020f50, 'cpu_hz': 104000000})
    browser.uart(0, b'cached firmware text\n')
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1100, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        yield page
        browser.close()
        assert not errors


def ready(page, view):
    page.goto(view.url)
    page.wait_for_function('() => handset3D?.renders > 0', timeout=30000)
    expect(page.locator('#connection')).to_have_attribute('aria-label', 'Connected')


def project(page, action, dx=0, dy=0):
    return page.evaluate('''({action, dx, dy}) => {
      const v = handset3D, mesh = v.keys.get(action) || v.joystick;
      mesh.geometry.computeBoundingBox();
      const p = mesh.geometry.boundingBox.getCenter(v.camera.position.clone());
      mesh.localToWorld(p); p.x += dx; p.y += dy; p.project(v.camera);
      const r = v.canvas.getBoundingClientRect();
      return {x:r.left+(p.x+1)*r.width/2, y:r.top+(1-p.y)*r.height/2};
    }''', {'action': action, 'dx': dx, 'dy': dy})


def transitions(view):
    return [(key, down) for key, down, _ in view.input.take() if key is not None]


def framing(page):
    return page.evaluate('''async () => {
      const {Box3, Vector3}=await import('/vendor/three.module.js'), v=handset3D;
      v.camera.updateMatrixWorld();
      const center=new Box3().setFromObject(v.screen).getCenter(new Vector3());
      const lcd=center.clone().project(v.camera), r=v.canvas.getBoundingClientRect();
      const neutral=v.camera.clone(); neutral.position.sub(v.controls.target); neutral.updateMatrixWorld();
      const bounds=new Box3().setFromObject(v.model); let extent=0;
      for (const x of [bounds.min.x,bounds.max.x]) for (const y of [bounds.min.y,bounds.max.y])
        for (const z of [bounds.min.z,bounds.max.z]) {
          const p=new Vector3(x,y,z).project(neutral); extent=Math.max(extent,Math.abs(p.x),Math.abs(p.y));
        }
      return {center:center.toArray(), target:v.controls.target.toArray(), lcd:lcd.toArray(),
        weight:v.controls.target.y/center.y, neutralExtent:extent, position:v.camera.position.toArray(),
        distance:v.controls.getDistance(), min:v.controls.minDistance, fit:v.fit,
        angles:[v.controls.getAzimuthalAngle(),v.controls.getPolarAngle()],
        pixel:{x:r.left+(lcd.x+1)*r.width/2,y:r.top+(1-lcd.y)*r.height/2},
        rect:{x:r.left,y:r.top,width:r.width,height:r.height}};
    }''')


def zoom(page, ratio):
    page.evaluate('''ratio => { const v=handset3D;
      v.camera.position.sub(v.controls.target).setLength(v.fit*ratio).add(v.controls.target);
      v.controls.update();
    }''', ratio)


def test_live_lcd_orientation_pixels_unknown_and_final(page, view):
    ready(page, view)
    # Distinct quadrants expose mirroring, flipping and color-space errors.
    raw = b''.join((bytes.fromhex('f800')*88+bytes.fromhex('07e0')*88 if y < 110 else
                    bytes.fromhex('001f')*88+bytes.fromhex('f79e')*88) for y in range(220))
    view.display(panel(raw, frames=2))
    page.wait_for_function("() => el('lcd').getContext('2d').getImageData(0,0,1,1).data[0] === 255")
    colors = page.evaluate('''() => {
      const v=handset3D, gl=v.renderer.getContext(), r=v.canvas.getBoundingClientRect();
      const g=v.screen.geometry; g.computeBoundingBox(); const b=g.boundingBox;
      v.renderer.render(v.scene,v.camera);
      return [[.25,.75],[.75,.75],[.25,.25],[.75,.25]].map(([x,y]) => {
        const p=v.camera.position.clone().set(b.min.x+(b.max.x-b.min.x)*x,
          b.min.y+(b.max.y-b.min.y)*y,b.max.z);
        v.screen.localToWorld(p); p.project(v.camera);
        const out=new Uint8Array(4);
        gl.readPixels(Math.round((p.x+1)*gl.drawingBufferWidth/2),
          Math.round((p.y+1)*gl.drawingBufferHeight/2),1,1,gl.RGBA,gl.UNSIGNED_BYTE,out);
        return Array.from(out);
      });
    }''')
    for actual, expected in zip(colors, [[255,0,0,255], [0,255,0,255], [0,0,255,255], [246,242,246,255]]):
        assert actual == pytest.approx(expected, abs=1)
    view.display(panel(b'', frames=3, known=8))
    expect(page.locator('#unknown')).to_be_visible()
    page.wait_for_function('() => handset3D.activity.visible')
    angle = page.evaluate('() => handset3D.spinner.rotation.z')
    page.wait_for_timeout(100)
    assert page.evaluate('() => handset3D.spinner.rotation.z') != angle
    assert 'Waiting for a complete panel' not in page.locator('body').inner_text()
    view.display(panel(bytes.fromhex('001f')*(176*220), frames=4))
    view.finish({'success': True, 'phases': {}, 'snapshot': {'stop': {'reason': 'final'}}}, None)
    expect(page.locator('#outcome')).to_have_text('final')
    expect(page.locator('#unknown')).to_be_hidden()
    assert page.evaluate('() => handset3D.activity.visible') is False
    assert page.evaluate("() => Array.from(el('lcd').getContext('2d').getImageData(0,0,1,1).data)") == [0,0,255,255]
    assert page.evaluate('() => inputEnabled') is False
    page.wait_for_timeout(100)
    count = page.evaluate('() => handset3D.renders')
    page.wait_for_timeout(200)
    assert page.evaluate('() => handset3D.renders') == count


def test_all_mesh_contacts_hold_highlight_and_gesture_ownership(page, view):
    ready(page, view)
    offsets = {'up': (0,.027), 'right': (.027,0), 'down': (0,-.027), 'left': (-.027,0), 'select': (0,0)}
    view.input.take()
    for key in sorted(BUTTONS - {'volume-up', 'volume-down', 'play-pause', 'camera', 'camera-focus', 'power'}):
        point = project(page, key, *offsets.get(key, (0,0)))
        page.mouse.move(**point)
        expect(page.locator('#three-stage canvas')).to_have_attribute('title',
            page.locator(f'[data-key="{key}"]').get_attribute('title'))
        page.mouse.down()
        expect(page.locator('#three-stage canvas')).to_have_attribute('title', '')
        page.wait_for_function('(key) => sources.has("pointer:1") && sources.get("pointer:1") === key', arg=key)
        assert transitions(view) == [(key, True)]
        assert page.locator(f'[data-key="{key}"]').get_attribute('aria-pressed') == 'true'
        if key not in offsets:
            assert page.evaluate('(key) => handset3D.keys.get(key).material.emissive.r', key) == .85
        page.mouse.move(20, 200)
        page.mouse.up()
        page.wait_for_timeout(30)
        assert transitions(view) == [(key, False)]
    # A body drag can cross buttons, but cannot press them or leave a hold.
    before = page.evaluate('() => handset3D.camera.position.toArray()')
    page.mouse.move(200, 230); page.mouse.down(); page.mouse.move(650, 430, steps=8); page.mouse.up()
    assert not transitions(view)
    assert page.evaluate('() => handset3D.camera.position.toArray()') != before
    page.locator('#front-view').click()
    assert page.evaluate('() => Math.abs(handset3D.camera.position.x) < 1e-8')
    # Rear view cannot pick a front key through the casing.
    page.evaluate('''() => { const v=handset3D; v.camera.position.set(0,0,-v.fit); v.controls.update();
      v.renderer.render(v.scene,v.camera); }''')
    point = project(page, '5')
    page.mouse.click(**point)
    assert not transitions(view)


def test_native_hover_tip_clears_on_leave_camera_disconnect_and_disabled_input(page, view):
    ready(page, view)
    canvas=page.locator('#three-stage canvas')
    point=project(page, '5'); view.input.take()
    page.mouse.move(**point)
    expect(canvas).to_have_attribute('title', 'Number 5 · 5')
    page.mouse.move(**framing(page)['pixel'])
    expect(canvas).to_have_attribute('title', '')  # LCD/casing has no contact.
    page.mouse.move(**point)
    expect(canvas).to_have_attribute('title', 'Number 5 · 5')
    page.mouse.move(5,5)
    expect(canvas).to_have_attribute('title', '')
    page.mouse.move(**point)
    page.mouse.wheel(0,-100)
    expect(canvas).to_have_attribute('title', '')
    page.evaluate('() => handset3D.front()')
    page.mouse.move(**project(page, '5'))
    expect(canvas).to_have_attribute('title', 'Number 5 · 5')
    page.evaluate('() => inputSocket.close()')
    expect(canvas).to_have_attribute('title', '')
    page.wait_for_function('() => inputEnabled')
    page.mouse.move(5,5); page.mouse.move(**project(page, '5'))
    expect(canvas).to_have_attribute('title', 'Number 5 · 5')
    view.finish({'success':True,'phases':{},'snapshot':{'stop':{'reason':'final'}}},None)
    expect(canvas).to_have_attribute('title', '')
    assert not transitions(view)


def test_keyboard_semantics_shared_sources_and_release_on_reset(page, view):
    ready(page, view); view.input.take()
    point = project(page, '5'); page.mouse.move(**point); page.mouse.down()
    page.keyboard.down('5'); page.mouse.up(); page.wait_for_timeout(50)
    assert transitions(view) == [('5', True)]
    page.keyboard.up('5'); page.wait_for_timeout(30)
    assert transitions(view) == [('5', False)]
    # Tab/assistive controls keep their own Enter/Space meaning.
    page.locator('[data-key="2"]').focus()
    assert page.locator('#key-hint').count() == 0
    assert page.evaluate("() => handset3D.keys.get('2').material.emissive.r") == .4
    page.keyboard.press('Space'); page.wait_for_timeout(30)
    assert transitions(view) == [('2', True), ('2', False)]
    page.keyboard.press('Enter'); page.wait_for_timeout(30)
    assert transitions(view) == [('2', True), ('2', False)]
    page.keyboard.press('Control+Enter'); page.keyboard.press('Alt+Space'); page.wait_for_timeout(30)
    assert not transitions(view)
    page.locator('[data-key="2"]').evaluate('(button) => button.click()')
    page.wait_for_function('() => !sources.has("activate:2")')
    page.wait_for_timeout(30)
    assert transitions(view) == [('2', True), ('2', False)]
    page.locator('#handset').focus(); page.keyboard.down('3')
    page.evaluate('() => { handset3D.camera.position.x = .5; handset3D.controls.update(); }')
    page.locator('#front-view').click(); page.keyboard.up('3'); page.wait_for_timeout(30)
    assert transitions(view) == [('3', True), ('3', False)]
    assert page.evaluate('() => sources.size === 0 && handset3D.keyPointers.size === 0')
    expect(page.locator('#front-view')).to_be_hidden()


def test_side_and_power_meshes_camera_stages_and_cancel(page, view):
    ready(page, view)
    for key, direction in [('volume-up', [1, 0, .3]), ('volume-down', [1, 0, .3]),
                           ('camera', [1, 0, .3]), ('play-pause', [-1, 0, .3]),
                           ('power', [0, 1, .3])]:
        page.evaluate('''direction => {
          const v=handset3D; v.camera.position.fromArray(direction).normalize().multiplyScalar(v.fit);
          v.controls.update(); v.renderer.render(v.scene,v.camera);
        }''', direction)
        view.input.take()
        point = project(page, key)
        page.mouse.move(**point)
        expect(page.locator('#three-stage canvas')).to_have_attribute('title',
            page.locator(f'[data-key="{key}"]').get_attribute('title'))
        page.mouse.down()
        page.wait_for_function('(key) => sources.get("pointer:1") === key', arg=key)
        page.wait_for_timeout(30)
        assert transitions(view) == [(key, True)]
        assert page.evaluate('(key) => handset3D.keys.get(key).material.emissive.r', key) == .85
        # Cancellation must release the contact even while the pointer is held.
        page.evaluate('window.dispatchEvent(new Event("blur"))')
        page.wait_for_function('() => sources.size === 0')
        page.mouse.up(); page.wait_for_timeout(30)
        assert transitions(view) == [(key, False)]

    page.evaluate('''() => { const v=handset3D;
      v.camera.position.set(v.fit,0,v.fit*.3); v.controls.update();
      v.renderer.render(v.scene,v.camera); }''')
    point = project(page, 'camera')
    page.keyboard.down('Shift'); page.mouse.move(**point)
    expect(page.locator('#three-stage canvas')).to_have_attribute('title',
        'Camera half press · Shift+C or Shift+click camera')
    page.mouse.down()
    page.wait_for_function('() => sources.get("pointer:1") === "camera-focus"')
    page.wait_for_timeout(30)
    assert transitions(view) == [('camera-focus', True)]
    assert page.evaluate('() => handset3D.keys.get("camera").material.emissive.r') == .85
    page.keyboard.up('Shift'); page.mouse.up(); page.wait_for_timeout(30)
    assert transitions(view) == [('camera-focus', False)]
    # Keyboard/assistive focus reveals controls on their physical side.
    for key, axis, sign in [('volume-up', 'x', 1), ('volume-down', 'x', 1),
                            ('camera', 'x', 1), ('camera-focus', 'x', 1),
                            ('play-pause', 'x', -1), ('power', 'y', 1)]:
        page.locator(f'[data-key="{key}"]').focus()
        assert page.evaluate('({axis,sign}) => handset3D.camera.position[axis]*sign > 0',
                             {'axis': axis, 'sign': sign})
        page.keyboard.press('Space'); page.wait_for_timeout(30)
        assert transitions(view) == [(key, True), (key, False)]


@pytest.mark.parametrize('fault', ['module', 'model', 'webgl'])
def test_loading_failure_does_not_block_connection_or_keyboard(page, view, fault):
    if fault in ('module', 'model'):
        page.route('**/'+('handset3d.js' if fault == 'module' else 'model/w800i.glb'), lambda route: route.abort())
    elif fault == 'webgl':
        page.add_init_script('''const original=HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext=function(kind,...args) {
            return kind.startsWith('webgl') ? null : original.call(this,kind,...args);
          };''')
    page.goto(view.url)
    expect(page.locator('#connection')).to_have_attribute('aria-label', 'Connected')
    expect(page.locator('#observations')).to_have_text('100.0 M')
    expect(page.locator('#uart')).to_have_text('cached firmware text\n')
    expect(page.locator('#view-state')).to_have_text('3D handset unavailable. Reload to try again.', timeout=30000)
    expect(page.locator('#front-view')).to_be_hidden()
    assert page.locator('#three-stage canvas').count() == 0
    assert page.locator('#view-2d, #view-3d, .screen, .speaker').count() == 0
    page.locator('#handset').focus(); view.input.take()
    page.keyboard.press('5'); page.wait_for_timeout(30)
    assert transitions(view) == [('5', True), ('5', False)]


def test_context_loss_reconnect_and_bounded_idle_resources(page, view):
    ready(page, view)
    page.mouse.move(30, 200); page.wait_for_timeout(80)
    count = page.evaluate('() => handset3D.renders')
    page.wait_for_timeout(550)
    assert page.evaluate('() => handset3D.renders') == count
    memory = page.evaluate('() => ({...handset3D.renderer.info.memory})')
    for _ in range(5):
        page.evaluate('() => { handset3D.camera.position.x = .5; handset3D.controls.update(); }')
        page.locator('#front-view').click()
    assert page.locator('#three-stage canvas').count() == 1
    assert page.evaluate('() => ({...handset3D.renderer.info.memory})') == memory
    async def disconnect():
        await asyncio.gather(*(ws.close() for ws in list(view.clients)))
    point = project(page, '1'); page.mouse.move(**point); page.mouse.down()
    asyncio.run_coroutine_threadsafe(disconnect(), view.loop).result(5)
    page.wait_for_function('() => sources.size === 0 && handset3D.keyPointers.size === 0')
    page.mouse.up()
    expect(page.locator('#connection')).to_have_attribute('aria-label', 'Connected', timeout=5000)
    page.evaluate('''() => {
      window.rearReflectionDisposed = false;
      handset3D.rearEnvironment.addEventListener('dispose', () => { window.rearReflectionDisposed = true; });
    }''')
    page.evaluate("() => handset3D.renderer.getContext().getExtension('WEBGL_lose_context').loseContext()")
    expect(page.locator('#view-state')).to_have_text('3D handset unavailable. Reload to try again.')
    assert page.evaluate('() => window.rearReflectionDisposed')
    assert page.locator('#three-stage canvas').count() == 0
    expect(page.locator('#front-view')).to_be_hidden()
    page.reload()
    page.wait_for_function('() => handset3D?.renders > 0', timeout=30000)


def test_delayed_module_keeps_updates_and_ignores_old_2d_preference(page, view):
    held = []
    page.add_init_script("localStorage.setItem('db2010.handset-view', '2d')")
    page.route('**/handset3d.js', lambda route: held.append(route))
    page.goto(view.url)
    expect(page.locator('#view-state')).to_have_text('Loading 3D…')
    expect(page.locator('#front-view')).to_be_hidden()
    expect(page.locator('#connection')).to_have_attribute('aria-label', 'Connected')
    view.progress({'observations': 200000000, 'pc': 0x44020f50})
    view.uart(0, b'updates during model loading\n')
    expect(page.locator('#observations')).to_have_text('200.0 M')
    expect(page.locator('#uart')).to_contain_text('updates during model loading')
    assert page.locator('#view-2d, #view-3d').count() == 0
    held[0].continue_()
    page.wait_for_function('() => handset3D?.renders > 0', timeout=30000)
    expect(page.locator('#view-state')).to_be_empty()
    expect(page.locator('#front-view')).to_be_hidden()


def test_front_button_only_appears_when_rotated(page, view):
    ready(page, view)
    button = page.locator('#front-view')
    expect(button).to_be_hidden()
    assert page.locator('.view-controls button').count() == 1
    assert 'Drag to rotate' not in page.locator('body').inner_text()
    # Zoom alone keeps the default orientation and needs no front-view reset.
    page.mouse.move(200, 230); page.mouse.wheel(0, -200)
    expect(button).to_be_hidden()
    page.mouse.down(); page.mouse.move(500, 350, steps=8); page.mouse.up()
    expect(button).to_be_visible()
    page.set_viewport_size({'width': 800, 'height': 800})
    expect(button).to_be_visible()
    button.click()
    expect(button).to_be_hidden()
    assert page.evaluate('() => Math.abs(handset3D.camera.position.z-handset3D.fit) < 1e-8')
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(button).to_be_hidden()


@pytest.mark.parametrize('viewport', [(1100,900), (390,844), (350,1000)])
def test_zoom_framing_is_smooth_reversible_and_centers_lcd(page, view, viewport):
    page.set_viewport_size({'width': viewport[0], 'height': viewport[1]})
    ready(page, view)
    initial=framing(page)
    assert initial['target'] == [0,0,0]
    assert initial['neutralExtent'] < 1
    weights=[]
    for i in range(31):
        zoom(page, 1-.55*i/30)
        state=framing(page)
        if state['neutralExtent'] <= 1:
            assert state['target'] == [0,0,0]
        else:
            assert state['weight'] > 0
        weights.append(state['weight'])
        assert state['angles'] == pytest.approx(initial['angles'], abs=1e-10)
    assert weights == sorted(weights)
    assert max(b-a for a,b in zip(weights,weights[1:])) < .15
    assert any(0 < w < 1 for w in weights)
    assert state['target'] == pytest.approx(state['center'], abs=1e-10)
    assert state['lcd'][:2] == pytest.approx([0,0], abs=1e-10)
    for _ in range(4):
        zoom(page, 1); zoom(page, .45)
    zoom(page, 1)
    assert framing(page)['position'] == pytest.approx(initial['position'], abs=1e-10)
    assert framing(page)['target'] == [0,0,0]
    expect(page.locator('#front-view')).to_be_hidden()


def test_wheel_close_zoom_preserves_oblique_angle_resize_and_reset(page, view):
    ready(page, view)
    page.evaluate('''() => { const v=handset3D;
      v.camera.position.set(v.fit*.3,v.fit*.1,v.fit); v.controls.update(); }''')
    before=framing(page)
    page.mouse.move(before['rect']['x']+10,before['rect']['y']+10)
    page.mouse.wheel(0,-2500)
    page.wait_for_function('() => handset3D.controls.getDistance() <= handset3D.controls.minDistance*1.00001')
    close=framing(page)
    assert close['angles'] == pytest.approx(before['angles'], abs=1e-10)
    assert close['lcd'][:2] == pytest.approx([0,0], abs=1e-10)
    # Picking uses the translated camera; a visible key still owns its hold.
    point=project(page, 'soft-left'); view.input.take()
    page.mouse.move(**point); page.mouse.down()
    page.wait_for_function('() => sources.get("pointer:1") === "soft-left"')
    page.mouse.up(); page.wait_for_timeout(30)
    assert transitions(view) == [('soft-left',True),('soft-left',False)]
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_function('() => handset3D.camera.aspect < 1')
    resized=framing(page)
    assert resized['distance']/resized['fit'] == pytest.approx(.45)
    assert resized['lcd'][:2] == pytest.approx([0,0], abs=1e-10)
    assert resized['angles'] == pytest.approx(before['angles'], abs=1e-10)
    page.locator('#front-view').click()
    reset=framing(page)
    assert reset['target'] == [0,0,0]
    assert reset['position'] == pytest.approx([0,0,reset['fit']], abs=1e-10)
    page.mouse.move(10,100); page.wait_for_timeout(100)
    renders=page.evaluate('() => handset3D.renders')
    page.wait_for_timeout(250)
    assert page.evaluate('() => handset3D.renders') == renders


def test_pinch_close_zoom_centers_lcd_and_zoom_out_restores_body(page, view):
    page.set_viewport_size({'width':390,'height':844})
    ready(page, view)
    session=page.context.new_cdp_session(page)
    session.send('Emulation.setTouchEmulationEnabled',{'enabled':True,'maxTouchPoints':5})
    initial=framing(page); rect=initial['rect']; y=initial['pixel']['y']
    def touches(fraction):
        return [{'id':0,'x':rect['x']+rect['width']*(.5-fraction),'y':y},
                {'id':1,'x':rect['x']+rect['width']*(.5+fraction),'y':y}]
    view.input.take()
    for start,end in [(.02,.4),(.4,.02)]:
        session.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':touches(start)})
        session.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':touches(end)})
        session.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        if start == .02:
            close=framing(page)
            assert close['distance'] == pytest.approx(close['min'])
            assert close['lcd'][:2] == pytest.approx([0,0], abs=1e-10)
            assert close['angles'] == pytest.approx(initial['angles'], abs=1e-10)
        else:
            assert framing(page)['target'] == [0,0,0]
    assert not transitions(view)


def test_narrow_touch_holds_cancel_and_pinch(page, view):
    page.set_viewport_size({'width': 390, 'height': 844})
    ready(page, view)
    assert page.evaluate('() => document.documentElement.scrollWidth') <= 390
    session = page.context.new_cdp_session(page)
    session.send('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
    first, second = project(page, '2'), project(page, '3')
    touches = [dict(id=0, **first), dict(id=1, **second)]
    view.input.take()
    session.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': touches[:1]})
    session.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': touches})
    page.wait_for_function('() => handset3D.keyPointers.size === 2')
    expect(page.locator('#three-stage canvas')).to_have_js_property('title', '')
    assert transitions(view) == [('2', True), ('3', True)]
    session.send('Input.dispatchTouchEvent', {'type': 'touchCancel', 'touchPoints': []})
    page.wait_for_function('() => handset3D.keyPointers.size === 0')
    assert set(transitions(view)) == {('2', False), ('3', False)}
    distance = page.evaluate('() => handset3D.camera.position.length()')
    session.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [
        {'id':0, 'x':30, 'y':250}, {'id':1, 'x':360, 'y':250}]})
    session.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [
        {'id':0, 'x':10, 'y':250}, {'id':1, 'x':380, 'y':250}]})
    session.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    assert page.evaluate('() => handset3D.camera.position.length()') < distance
    assert not transitions(view)


def test_high_density_rendering_is_capped_at_two(page, view):
    context = page.context.browser.new_context(viewport={'width':390,'height':844}, device_scale_factor=3)
    try:
        dense = context.new_page()
        ready(dense, view)
        assert dense.evaluate('() => devicePixelRatio') == 3
        assert dense.evaluate('() => handset3D.renderer.getPixelRatio()') == 2
        assert dense.evaluate('() => handset3D.canvas.width') == dense.locator('#three-stage').bounding_box()['width']*2
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'firefox'])
def test_forwarded_default_view_and_resources(view, engine):
    import select
    import socket
    import socketserver
    import threading

    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.create_connection(('127.0.0.1', int(view.url.rsplit(':',1)[1]))) as backend:
                peers = (self.request, backend)
                while True:
                    readable, _, _ = select.select(peers, [], [], 5)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        (backend if source is self.request else self.request).sendall(data)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with Server(('127.0.0.1', 0), Forward) as forward:
        worker = threading.Thread(target=forward.serve_forever)
        worker.start()
        try:
            with sync_playwright() as p:
                browser = getattr(p, engine).launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(f'http://localhost:{forward.server_address[1]}')
                    page.wait_for_function("() => el('view-state').textContent !== 'Loading 3D…'", timeout=30000)
                    expect(page.locator('#connection')).to_have_attribute('aria-label', 'Connected')
                    expect(page.locator('#uart')).to_have_text('cached firmware text\n')
                    if engine == 'chromium':
                        assert page.evaluate('() => handset3D?.renders > 0')
                    else:
                        # Headless Firefox may lack WebGL; keep a clear error
                        # while execution observations and keyboard input work.
                        assert page.evaluate('() => handset3D?.renders > 0') or page.locator('#view-state').inner_text() == '3D handset unavailable. Reload to try again.'
                    page.locator('#handset').focus(); view.input.take()
                    page.keyboard.press('Shift+Enter'); page.wait_for_timeout(50)
                    assert transitions(view) == [('walkman', True), ('walkman', False)]
                finally:
                    browser.close()
        finally:
            forward.shutdown(); worker.join(5)


@pytest.mark.parametrize('key,selected,mask,direction', [
    ('1', 0x37, 0x40, [0, 0, 1]),
    ('volume-up', 0x37, 0xb01, [1, 0, .3]),
    ('volume-down', 0x3b, 0xb01, [1, 0, .3]),
    ('play-pause', 0x3d, 0xb01, [-1, 0, .3]),
    ('camera-focus', 0x1f, 0xc01, [1, 0, .3]),
    ('camera', 0x1f, 0xc02, [1, 0, .3]),
    ('power', 0x3f, 0x40, [0, 1, .3]),
])
def test_3d_pointer_reaches_native_qemu_scanner(tmp_path, monkeypatch, key, selected, mask, direction):
    import threading
    from pathlib import Path
    from db2010.execution import runner
    from db2010.paths import ROOT
    from db2010.diagnostics.artifacts import artifact
    from support.fixtures import base_state, words, freeze_state

    # Controlled ARM fixture polls the selected external input until press and
    # release, then reaches a known guard. This is not original boot firmware.
    code = [0xe59f1030,0xe59f0030,0xe1c100b0,0xe2811002,0xe3a00037,0xe1c100b0,
            0xe1d120b0,0xe3120040,0x1afffffc,0xe1d120b0,0xe3120040,0x0afffffc,
            0xe3a0002a,0xe1d120b2,0xfb010000,0x3a1]
    code[4] = 0xe3a00000 | selected
    code[7] = code[10] = 0xe3120000 | mask

    def prepare(root, spec, output, **options):
        state = base_state(words(*code), limit=1000000)
        state['keypad'] = {'profile': 'w800-front-ideal-contacts'}
        return freeze_state(state, output), {'seconds': 10}

    monkeypatch.setattr(runner, 'prepare', prepare)
    view = Browser(0)
    result = []
    worker = threading.Thread(target=lambda: result.append(guarded_run(ROOT, Path('unused'), tmp_path/'run',
        diagnostics=True, seconds=10, config_limits=False, keypad_input=view.input, console=view)))
    try:
        worker.start()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                ready(page, view)
                page.wait_for_function('() => inputEnabled')
                page.evaluate('''direction => { const v=handset3D;
                  v.camera.position.fromArray(direction).normalize().multiplyScalar(v.fit);
                  v.controls.update(); v.renderer.render(v.scene,v.camera);
                }''', direction)
                if key == 'camera-focus':
                    page.keyboard.down('Shift')
                point = project(page, 'camera' if key == 'camera-focus' else key)
                page.mouse.move(**point); page.mouse.down(); page.wait_for_timeout(200); page.mouse.up()
                if key == 'camera-focus':
                    page.keyboard.up('Shift')
                worker.join(10)
                assert not worker.is_alive()
                outcome, failure = result[0]
                assert failure is None and outcome['snapshot']['registers'][0] == 42
                assert outcome['snapshot']['keypad']['pressed'] == []
                rows = [json.loads(line) for line in artifact(tmp_path/'run', 'keypad-input.log').read_text().splitlines()]
                assert [row['held'] for row in rows] == [[key], []]
                assert rows[1]['virtual_ns'] > rows[0]['virtual_ns']
            finally:
                browser.close()
    finally:
        worker.join(12)
        view.close()
