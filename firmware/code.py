import time

try:
    import board
    import busio
    import displayio
    import keypad
    import rotaryio
    import terminalio
    import usb_hid
    from adafruit_display_text import label
    from adafruit_displayio_ssd1306 import SSD1306
    from adafruit_hid.consumer_control import ConsumerControl
    from adafruit_hid.consumer_control_code import ConsumerControlCode
    from adafruit_hid.keyboard import Keyboard
    from adafruit_hid.keycode import Keycode
    from adafruit_hid.keyboard_layout_us import KeyboardLayoutUS

    CIRCUITPYTHON_READY = True
except ImportError as import_error:
    CIRCUITPYTHON_READY = False
    print("CircuitPython libraries not available:", import_error)


DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 32
DISPLAY_I2C_ADDRESS = 0x3C
COLUMNS_TO_ANODES = True

IDLE_TIMEOUT_SECONDS = 15
METRIC_OVERLAY_SECONDS = 2
PERCENT_STEP = 5

LAYER_PRODUCTIVITY = 0
LAYER_MEDIA = 1
LAYER_HOME = 2
LAYER_NAMES = ("PRODUCTIVITY", "MEDIA", "HOME")

ENCODER2_MODE_BRIGHTNESS = 0
ENCODER2_MODE_FOCUS_VOLUME = 1
ENCODER2_MODE_NAMES = ("BRIGHTNESS", "FOCUS_VOLUME")
ENCODER2_MODE_SHORT_NAMES = ("BRIGHT", "FOCUS")

SPOTLIGHT_OPEN_DELAY = 0.25
SPOTLIGHT_TYPE_DELAY = 0.10

SLIDER_X = 10
SLIDER_Y = 26
SLIDER_WIDTH = 108
SLIDER_HEIGHT = 6


current_layer = LAYER_PRODUCTIVITY
encoder2_mode = ENCODER2_MODE_BRIGHTNESS

master_volume_percent = 50
brightness_percent = 50
focus_volume_percent = 50

last_interaction_time = time.monotonic()
metric_overlay_until = 0.0
metric_title = ""
metric_percent = 0
last_display_signature = None
last_display_state = None

keyboard = None
keyboard_layout = None
consumer_control = None
matrix = None
encoder1 = None
encoder2 = None
encoder1_last_position = 0
encoder2_last_position = 0

display = None
title_label = None
detail_label = None
slider_bitmap = None


def debug(message):
    print("[DEBUG]", message)


def get_pin(name):
    try:
        return getattr(board, name)
    except AttributeError:
        debug("Pin {} is not available on this board.".format(name))
        return None


MATRIX_ROWS = (get_pin("D1"), get_pin("D2")) if CIRCUITPYTHON_READY else ()
MATRIX_COLUMNS = (get_pin("D3"), get_pin("D4"), get_pin("D5")) if CIRCUITPYTHON_READY else ()
ENCODER1_A_PIN = get_pin("D8") if CIRCUITPYTHON_READY else None
ENCODER1_B_PIN = get_pin("D9") if CIRCUITPYTHON_READY else None
ENCODER2_A_PIN = get_pin("D10") if CIRCUITPYTHON_READY else None
ENCODER2_B_PIN = get_pin("D11") if CIRCUITPYTHON_READY else None


def clamp(value, low, high):
    return max(low, min(high, value))


def record_interaction(_source=None):
    global last_interaction_time

    last_interaction_time = time.monotonic()


def set_metric_overlay(title, percent):
    global metric_title
    global metric_percent
    global metric_overlay_until

    metric_title = title
    metric_percent = clamp(percent, 0, 100)
    metric_overlay_until = time.monotonic() + METRIC_OVERLAY_SECONDS


def tap_keys(*keycodes):
    if keyboard is None:
        debug("Keyboard HID is not ready.")
        return

    keyboard.send(*keycodes)


def send_text(text):
    if keyboard_layout is None:
        debug("Keyboard layout is not ready.")
        return

    keyboard_layout.write(text)


def send_consumer(code):
    if consumer_control is None:
        debug("ConsumerControl HID is not ready.")
        return

    consumer_control.send(code)


def action_copy():
    debug("Action: Copy")
    tap_keys(Keycode.COMMAND, Keycode.C)


def action_paste():
    debug("Action: Paste")
    tap_keys(Keycode.COMMAND, Keycode.V)


def action_lock_screen():
    debug("Action: Lock Screen")
    tap_keys(Keycode.CONTROL, Keycode.COMMAND, Keycode.Q)


def action_quit_application():
    debug("Action: Quit Application")
    tap_keys(Keycode.COMMAND, Keycode.Q)


def action_backtrack():
    debug("Action: Backtrack")
    send_consumer(ConsumerControlCode.SCAN_PREVIOUS_TRACK)


def action_skip():
    debug("Action: Skip")
    send_consumer(ConsumerControlCode.SCAN_NEXT_TRACK)


def action_play_pause():
    debug("Action: Play/Pause")
    send_consumer(ConsumerControlCode.PLAY_PAUSE)


def action_open_spotify():
    debug("Action: Open Spotify")
    if keyboard is None or keyboard_layout is None:
        debug("Spotify launch skipped because keyboard HID is not ready.")
        return

    tap_keys(Keycode.COMMAND, Keycode.SPACE)
    time.sleep(SPOTLIGHT_OPEN_DELAY)
    send_text("Spotify")
    time.sleep(SPOTLIGHT_TYPE_DELAY)
    tap_keys(Keycode.ENTER)


def action_toggle_encoder2_mode():
    toggle_encoder2_mode()


def action_switch_layout():
    cycle_layer()


def make_home_action(index):
    def _action():
        debug("HOME placeholder action for key {}".format(index))

    return _action


# Key order is row-major:
# key 1, key 2, key 3
# key 4, key 5, key 6
#
# Fixed system keys:
# key 3 -> Encoder 2 Mode
# key 6 -> Switch Layout
LAYER_ACTIONS = {
    LAYER_PRODUCTIVITY: (
        action_copy,
        action_paste,
        action_toggle_encoder2_mode,
        action_lock_screen,
        action_quit_application,
        action_switch_layout,
    ),
    LAYER_MEDIA: (
        action_backtrack,
        action_skip,
        action_toggle_encoder2_mode,
        action_play_pause,
        action_open_spotify,
        action_switch_layout,
    ),
    LAYER_HOME: (
        make_home_action(1),
        make_home_action(2),
        action_toggle_encoder2_mode,
        make_home_action(4),
        make_home_action(5),
        action_switch_layout,
    ),
}


def current_time_text():
    try:
        now = time.localtime()
        if now.tm_year < 2024:
            return "--:--"
        return "{:02d}:{:02d}".format(now.tm_hour, now.tm_min)
    except Exception as error:
        debug("Time read failed: {}".format(error))
        return "--:--"


def center_text(text_label, text, y):
    text_label.text = text
    text_label.y = y

    if text:
        bounds = text_label.bounding_box
        text_label.x = max(0, (DISPLAY_WIDTH - bounds[2]) // 2)
    else:
        text_label.x = 0


def clear_slider():
    if slider_bitmap is None:
        return

    for x in range(SLIDER_WIDTH):
        for y in range(SLIDER_HEIGHT):
            slider_bitmap[x, y] = 0


def draw_slider(percent):
    if slider_bitmap is None:
        return

    clear_slider()

    for x in range(SLIDER_WIDTH):
        slider_bitmap[x, 0] = 1
        slider_bitmap[x, SLIDER_HEIGHT - 1] = 1

    for y in range(SLIDER_HEIGHT):
        slider_bitmap[0, y] = 1
        slider_bitmap[SLIDER_WIDTH - 1, y] = 1

    fill_width = int((SLIDER_WIDTH - 2) * clamp(percent, 0, 100) / 100)
    for x in range(1, fill_width + 1):
        for y in range(1, SLIDER_HEIGHT - 1):
            slider_bitmap[x, y] = 1


def display_state_signature():
    now = time.monotonic()

    if now < metric_overlay_until and metric_title:
        return "METRIC", ("METRIC", metric_title, metric_percent)

    if now - last_interaction_time <= IDLE_TIMEOUT_SECONDS:
        return "LAYOUT", ("LAYOUT", LAYER_NAMES[current_layer], ENCODER2_MODE_SHORT_NAMES[encoder2_mode])

    return "TIME", ("TIME", current_time_text())


def update_display(force=False):
    global last_display_signature
    global last_display_state

    if display is None or title_label is None or detail_label is None:
        return

    state_name, signature = display_state_signature()
    if not force and signature == last_display_signature:
        return

    if state_name != last_display_state:
        debug("Display -> {}".format(state_name))

    if state_name == "TIME":
        center_text(title_label, current_time_text(), 16)
        center_text(detail_label, "", 0)
        clear_slider()
    elif state_name == "LAYOUT":
        center_text(title_label, LAYER_NAMES[current_layer], 8)
        center_text(detail_label, "E2:{}".format(ENCODER2_MODE_SHORT_NAMES[encoder2_mode]), 18)
        clear_slider()
    else:
        center_text(title_label, metric_title, 8)
        center_text(detail_label, "{}%".format(metric_percent), 18)
        draw_slider(metric_percent)

    last_display_signature = signature
    last_display_state = state_name


def set_layer(new_layer):
    global current_layer

    current_layer = new_layer % len(LAYER_NAMES)
    debug("Layer changed to {}".format(LAYER_NAMES[current_layer]))
    record_interaction("layer change")


def cycle_layer():
    set_layer(current_layer + 1)


def toggle_encoder2_mode():
    global encoder2_mode

    encoder2_mode = (encoder2_mode + 1) % len(ENCODER2_MODE_NAMES)
    debug("Encoder 2 mode changed to {}".format(ENCODER2_MODE_NAMES[encoder2_mode]))
    record_interaction("encoder 2 mode change")


def handle_key_press(key_number):
    actions = LAYER_ACTIONS.get(current_layer, ())
    debug("Key {} pressed on {}".format(key_number + 1, LAYER_NAMES[current_layer]))
    record_interaction("key press")

    if 0 <= key_number < len(actions):
        actions[key_number]()
    else:
        debug("No action assigned for key {}".format(key_number + 1))


def handle_matrix_events():
    if matrix is None:
        return

    event = matrix.events.get()
    while event:
        if event.pressed:
            handle_key_press(event.key_number)
        event = matrix.events.get()


def apply_percent_change(current_value, delta):
    return clamp(current_value + (delta * PERCENT_STEP), 0, 100)


def handle_encoder1_delta(delta):
    global master_volume_percent

    if delta == 0:
        return

    direction = "up" if delta > 0 else "down"
    master_volume_percent = apply_percent_change(master_volume_percent, delta)

    debug(
        "Encoder 1 rotation: master volume {} -> {}%".format(
            direction, master_volume_percent
        )
    )
    record_interaction("encoder 1 rotation")
    set_metric_overlay("VOLUME", master_volume_percent)

    for _ in range(abs(delta)):
        if delta > 0:
            send_consumer(ConsumerControlCode.VOLUME_INCREMENT)
        else:
            send_consumer(ConsumerControlCode.VOLUME_DECREMENT)


def handle_encoder2_delta(delta):
    global brightness_percent
    global focus_volume_percent

    if delta == 0:
        return

    direction = "up" if delta > 0 else "down"
    record_interaction("encoder 2 rotation")

    if encoder2_mode == ENCODER2_MODE_BRIGHTNESS:
        brightness_percent = apply_percent_change(brightness_percent, delta)
        debug(
            "Encoder 2 rotation: brightness {} -> {}%".format(
                direction, brightness_percent
            )
        )
        set_metric_overlay("BRIGHTNESS", brightness_percent)

        code_name = "BRIGHTNESS_INCREMENT" if delta > 0 else "BRIGHTNESS_DECREMENT"
        code = getattr(ConsumerControlCode, code_name, None)
        if code is None:
            debug("Consumer control code {} is not available; using debug only.".format(code_name))
        else:
            for _ in range(abs(delta)):
                send_consumer(code)
    else:
        focus_volume_percent = apply_percent_change(focus_volume_percent, delta)
        debug(
            "Encoder 2 rotation: focus volume {} -> {}%".format(
                direction, focus_volume_percent
            )
        )
        set_metric_overlay("FOCUS VOL", focus_volume_percent)


def poll_encoders():
    global encoder1_last_position
    global encoder2_last_position

    if encoder1 is not None:
        position = encoder1.position
        delta = position - encoder1_last_position
        if delta:
            handle_encoder1_delta(delta)
            encoder1_last_position = position

    if encoder2 is not None:
        position = encoder2.position
        delta = position - encoder2_last_position
        if delta:
            handle_encoder2_delta(delta)
            encoder2_last_position = position


def init_hid():
    global keyboard
    global keyboard_layout
    global consumer_control

    try:
        keyboard = Keyboard(usb_hid.devices)
        keyboard_layout = KeyboardLayoutUS(keyboard)
        consumer_control = ConsumerControl(usb_hid.devices)
        debug("USB HID initialized.")
    except Exception as error:
        debug("USB HID init failed: {}".format(error))


def init_matrix():
    global matrix

    if not MATRIX_ROWS or not MATRIX_COLUMNS or None in MATRIX_ROWS or None in MATRIX_COLUMNS:
        debug("Matrix init skipped because one or more matrix pins are missing.")
        return

    try:
        matrix = keypad.KeyMatrix(
            row_pins=MATRIX_ROWS,
            column_pins=MATRIX_COLUMNS,
            columns_to_anodes=COLUMNS_TO_ANODES,
        )
        debug("Key matrix initialized.")
    except Exception as error:
        debug("Key matrix init failed: {}".format(error))


def init_encoders():
    global encoder1
    global encoder2
    global encoder1_last_position
    global encoder2_last_position

    if ENCODER1_A_PIN is not None and ENCODER1_B_PIN is not None:
        try:
            encoder1 = rotaryio.IncrementalEncoder(ENCODER1_A_PIN, ENCODER1_B_PIN)
            encoder1_last_position = encoder1.position
            debug("Encoder 1 initialized.")
        except Exception as error:
            debug("Encoder 1 init failed: {}".format(error))
    else:
        debug("Encoder 1 init skipped because one or more pins are missing.")

    if ENCODER2_A_PIN is not None and ENCODER2_B_PIN is not None:
        try:
            encoder2 = rotaryio.IncrementalEncoder(ENCODER2_A_PIN, ENCODER2_B_PIN)
            encoder2_last_position = encoder2.position
            debug("Encoder 2 initialized.")
        except Exception as error:
            debug("Encoder 2 init failed: {}".format(error))
    else:
        debug("Encoder 2 init skipped because one or more pins are missing.")


def init_display():
    global display
    global title_label
    global detail_label
    global slider_bitmap

    try:
        displayio.release_displays()
        i2c = busio.I2C(board.SCL, board.SDA)
        display_bus = displayio.I2CDisplay(i2c, device_address=DISPLAY_I2C_ADDRESS)
        display = SSD1306(display_bus, width=DISPLAY_WIDTH, height=DISPLAY_HEIGHT)

        slider_palette = displayio.Palette(2)
        slider_palette[0] = 0x000000
        slider_palette[1] = 0xFFFFFF

        slider_bitmap = displayio.Bitmap(SLIDER_WIDTH, SLIDER_HEIGHT, 2)
        slider = displayio.TileGrid(
            slider_bitmap,
            pixel_shader=slider_palette,
            x=SLIDER_X,
            y=SLIDER_Y,
        )

        title_label = label.Label(terminalio.FONT, text="", color=0xFFFFFF, x=0, y=8)
        detail_label = label.Label(terminalio.FONT, text="", color=0xFFFFFF, x=0, y=18)

        root_group = displayio.Group()
        root_group.append(title_label)
        root_group.append(detail_label)
        root_group.append(slider)

        display.root_group = root_group
        clear_slider()
        debug("OLED display initialized.")
        update_display(force=True)
    except Exception as error:
        debug("Display init failed: {}".format(error))
        display = None


def setup():
    debug("Starting macropad firmware setup...")
    init_hid()
    init_matrix()
    init_encoders()
    init_display()
    update_display(force=True)
    debug("Setup complete.")


def loop():
    while True:
        handle_matrix_events()
        poll_encoders()
        update_display()
        time.sleep(0.01)


if CIRCUITPYTHON_READY:
    setup()
    loop()
else:
    print("Copy this file to CIRCUITPY and run it there.")
