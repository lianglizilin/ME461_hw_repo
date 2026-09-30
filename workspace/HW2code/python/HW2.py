"""

ME 461 HW2 - LED Crossy Road



Board layout (LED1 is top right):



    LED5   LED4   LED3   LED2   LED1

    LED10  LED9   LED8   LED7   LED6

    LED15  LED14  LED13  LED12  LED11

    LED16



LED1-LED15 are a 5 x 3 view of the road.

    solid LED    = car

    blinking LED = you (always on the bottom row)



The cars do not move sideways.  The whole road scrolls toward you one

row at a time, and you move left and right to get through the gaps.

You blink fast just before the road scrolls: the LED right above you

must be off, or that car lands on you.



The four push-buttons, left to right on the board:



    LEFT   FREEZE   REVIVE   RIGHT



FREEZE stops the road for 2 seconds.  You can still move.

       2 uses per game.  LED16: solid = 2 left, slow blink = 1 left,

       off = none left, fast blink = time is frozen.

REVIVE after a crash the LEDs count down.  Press REVIVE before they

       run out to keep going.  1 use per game.



Keyboard (for testing without the board):

    Left / Right arrows = move, Down = freeze, Up = revive

"""

import json

import math

import os

import random

import time

import tkinter as tk

from tkinter import messagebox


import serial

# =============================================================

# Settings you may need to change

# =============================================================


# Change this to the COM port used by your RedBoard.

SERIAL_PORT = "COM82"

BAUD_RATE = 115200


# Which bit (0-3) of the value received from the RedBoard belongs to each

# action.  Bit 0 is push-button 1, bit 3 is push-button 4.

# If the buttons feel mirrored on your board, swap these numbers.

BUTTON_BITS = {
    "left": 3,
    "freeze": 2,
    "revive": 1,
    "right": 0,
}


# Set to 0 if a bit reads 0 while its button is held down.

BUTTON_PRESSED_VALUE = 1


# =============================================================

# Game tuning

# =============================================================


COLS = 5

ROWS = 3  # lanes shown on the board

PREVIEW_ROWS = 3  # extra lanes the UI can show (easy mode)


FREEZE_USES = 2  # FREEZE uses per game

FREEZE_S = 2.0  # how long one FREEZE lasts

REVIVE_USES = 1  # REVIVE uses per game

REVIVE_WINDOW_S = 3.0  # time to press REVIVE after a crash


SAFE_LANE_EVERY = 5  # every Nth lane has no cars

LANES_PER_LEVEL = 10  # speed goes up every N lanes

SPEEDUP_PER_LEVEL = 0.93  # scroll time is multiplied by this each level


WARNING_S = 0.4  # you blink fast this long before a scroll


# New lanes are only accepted if, from every free spot in the lane

# before, a spot that is clear ahead can be reached in this many steps.

# So there is always a way through: a crash is never forced.

MAX_DODGE = 2


# Seconds between scrolls at level 1, and the fastest it can get

DIFFICULTY = {
    "Easy": {"scroll": 1.5, "scroll_min": 0.80},
    "Normal": {"scroll": 1.1, "scroll_min": 0.55},
    "Hard": {"scroll": 0.8, "scroll_min": 0.40},
}


CRASH_PAUSE_S = 1.0  # flash time after the final crash

RESTART_LOCK_S = 1.0  # ignore buttons right after game over


TICK_MS = 20  # UI loop period

RESEND_S = 0.5  # resend the LED value at least this often


BOARD_MASK = 0x7FFF  # LED1-LED15

LED16_BIT = 15


BEST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crossy_best.json")


# UI colors

ROAD_COLOR = "#3b3b3b"

GRASS_COLOR = "#2f6b3f"

FOG_COLOR = "#1e1e1e"

CAR_COLOR = "#e5533d"

PLAYER_COLOR = "#ffd447"

LED_COLOR = "#4aa3ff"


def led_bit(row, col):
    """Bit index of the LED at (row, col).  row 0 = top, col 0 = left."""

    led_number = row * COLS + (COLS - col)

    return led_number - 1


def load_best():

    try:

        with open(BEST_FILE, "r") as f:

            data = json.load(f)

        if isinstance(data, dict):

            return {name: int(data.get(name, 0)) for name in DIFFICULTY}

    except (OSError, ValueError, TypeError):

        pass

    return {name: 0 for name in DIFFICULTY}


def save_best(best):

    try:

        with open(BEST_FILE, "w") as f:

            json.dump(best, f)

    except OSError:

        pass


# =============================================================

# Game logic (no UI or serial code in here)

# =============================================================


def lane_is_fair(prev, new):
    """True if every free cell of prev has a way forward into new.



    From each free cell the player may walk up to MAX_DODGE cells

    sideways through free cells of prev.  At least one cell reached

    that way must be free in new.

    """

    for col in range(COLS):

        if prev[col]:

            continue

        ok = not new[col]

        for step in (-1, 1):

            c = col

            for _ in range(MAX_DODGE):

                c += step

                if c < 0 or c >= COLS or prev[c]:

                    break

                if not new[c]:

                    ok = True

        if not ok:

            return False

    return True


class CrossyGame:

    def __init__(self):

        self.difficulty = "Normal"

        self.best = load_best()

        # "menu", "playing", "paused", "revive", "crashed", "gameover"

        self.state = "menu"

        self.state_time = 0.0

        self.reset()

    # ---------------------------------------------------------

    # Setup

    # ---------------------------------------------------------

    def reset(self):

        self.score = 0

        self.player_col = COLS // 2

        self.scroll_timer = 0.0  # time since the last scroll

        self.freezes_left = FREEZE_USES

        self.freeze_timer = 0.0

        self.revives_left = REVIVE_USES

        self.revive_timer = 0.0

        self.crash_timer = 0.0

        self.lanes_made = 0

        self.new_best = False

        # Each lane is a list of COLS bools, True = car.

        # lanes[0] is the lane the player stands on.

        self.lanes = [self.empty_lane(), self.empty_lane()]

        while len(self.lanes) < ROWS + PREVIEW_ROWS:

            self.lanes.append(self.new_lane())

    def set_state(self, state):

        self.state = state

        self.state_time = 0.0

    def start(self):

        self.reset()

        self.set_state("playing")

    def toggle_pause(self):

        if self.state == "playing":

            self.set_state("paused")

        elif self.state == "paused":

            self.set_state("playing")

    # ---------------------------------------------------------

    # Helpers

    # ---------------------------------------------------------

    @property
    def level(self):

        return self.score // LANES_PER_LEVEL

    def scroll_period(self):

        d = DIFFICULTY[self.difficulty]

        return max(d["scroll_min"], d["scroll"] * SPEEDUP_PER_LEVEL**self.level)

    def about_to_scroll(self):

        return self.scroll_period() - self.scroll_timer <= WARNING_S

    def empty_lane(self):

        return [False] * COLS

    def new_lane(self):

        self.lanes_made += 1

        if self.lanes_made % SAFE_LANE_EVERY == 0:

            return self.empty_lane()

        prev = self.lanes[-1]

        # More cars at higher levels, always at least 2 free cells.

        level = self.level

        if level < 2:

            low, high = 1, 2

        elif level < 5:

            low, high = 1, 3

        else:

            low, high = 2, 3

        for _ in range(100):

            cells = [False] * COLS

            for i in random.sample(range(COLS), random.randint(low, high)):

                cells[i] = True

            if lane_is_fair(prev, cells):

                return cells

        # Repeating the lane before is always fair.

        return list(prev)

    # ---------------------------------------------------------

    # Button input

    # ---------------------------------------------------------

    def press(self, action):

        # Any button starts the game from the menu or after game over.

        if self.state == "menu":

            self.start()

            return

        if self.state == "gameover":

            if self.state_time > RESTART_LOCK_S:

                self.start()

            return

        if self.state == "revive":

            if action == "revive":

                self.revive()

            return

        if self.state != "playing":

            return

        if action == "left":

            self.move(-1)

        elif action == "right":

            self.move(1)

        elif action == "freeze":

            if self.freezes_left > 0 and self.freeze_timer <= 0:

                self.freezes_left -= 1

                self.freeze_timer = FREEZE_S

    def move(self, step):

        col = self.player_col + step

        # The edges of the road are walls.

        if col < 0 or col >= COLS:

            return

        self.player_col = col

        self.check_crash()

    # ---------------------------------------------------------

    # Game step

    # ---------------------------------------------------------

    def update(self, dt):

        self.state_time += dt

        if self.state == "revive":

            self.revive_timer -= dt

            if self.revive_timer <= 0:

                self.finish()

            return

        if self.state == "crashed":

            self.crash_timer -= dt

            if self.crash_timer <= 0:

                self.finish()

            return

        if self.state != "playing":

            return

        # FREEZE: the road stops, the player can still move.

        if self.freeze_timer > 0:

            self.freeze_timer = max(0.0, self.freeze_timer - dt)

            return

        self.scroll_timer += dt

        while self.state == "playing" and self.scroll_timer >= self.scroll_period():

            self.scroll_timer -= self.scroll_period()

            self.scroll()

    def scroll(self):

        # The road moves one row toward the player.

        self.lanes.pop(0)

        self.lanes.append(self.new_lane())

        self.score += 1

        self.check_crash()

    def check_crash(self):

        if self.state != "playing":

            return

        if not self.lanes[0][self.player_col]:

            return

        if self.revives_left > 0:

            self.revive_timer = REVIVE_WINDOW_S

            self.set_state("revive")

        else:

            self.crash_timer = CRASH_PAUSE_S

            self.set_state("crashed")

    def revive(self):

        self.revives_left -= 1

        # Clear the player's lane and the lane right ahead.

        self.lanes[0] = self.empty_lane()

        self.lanes[1] = self.empty_lane()

        self.scroll_timer = 0.0

        self.freeze_timer = 0.0

        self.set_state("playing")

    def finish(self):

        if self.score > self.best[self.difficulty]:

            self.best[self.difficulty] = self.score

            self.new_best = True

            save_best(self.best)

        self.set_state("gameover")

    # ---------------------------------------------------------

    # LED output

    # ---------------------------------------------------------

    def led_frame(self, t):
        """Return the 16-bit LED value for time t (seconds)."""

        if self.state == "menu":

            # One light running through LED1-LED15

            return 1 << (int(t / 0.12) % (COLS * ROWS))

        if self.state == "revive":

            # Countdown: LED1-LED15 go out one by one

            fraction = max(0.0, self.revive_timer / REVIVE_WINDOW_S)

            lit = math.ceil(COLS * ROWS * fraction)

            return (1 << lit) - 1

        if self.state == "crashed":

            return BOARD_MASK if int(t / 0.1) % 2 == 0 else 0

        if self.state == "gameover":

            return BOARD_MASK if int(t / 0.5) % 2 == 0 else 0

        bits = 0

        # Cars.  Board row 0 (top) is the lane furthest ahead.

        for row in range(ROWS):

            lane = self.lanes[ROWS - 1 - row]

            for col in range(COLS):

                if lane[col]:

                    bits |= 1 << led_bit(row, col)

        # Player: slow blink, fast blink when the road is about to scroll

        player_on = True

        if self.state == "playing":

            half_period = 0.07 if self.about_to_scroll() else 0.2

            player_on = int(t / half_period) % 2 == 0

        player_bit = 1 << led_bit(ROWS - 1, self.player_col)

        if player_on:

            bits |= player_bit

        else:

            bits &= ~player_bit

        # LED16: FREEZE uses left

        if self.freeze_timer > 0:

            led16_on = int(t / 0.08) % 2 == 0

        elif self.freezes_left >= 2:

            led16_on = True

        elif self.freezes_left == 1:

            led16_on = int(t / 0.4) % 2 == 0

        else:

            led16_on = False

        if led16_on:

            bits |= 1 << LED16_BIT

        return bits


# =============================================================

# UI + serial

# =============================================================


class CrossyRoadGUI:

    CELL = 60

    BUTTON_ORDER = ["left", "freeze", "revive", "right"]

    def __init__(self, root):

        self.root = root

        self.root.title("RedBoard LED Crossy Road")

        self.root.resizable(False, False)

        self.game = CrossyGame()

        self.ser = None

        self.rx_buffer = bytearray()

        self.held = {action: 0 for action in BUTTON_BITS}

        self.last_frame = None

        self.last_send_time = 0.0

        self.last_time = time.monotonic()

        # ---------------------------------------------------------

        # Connect to RedBoard

        # ---------------------------------------------------------

        try:

            self.ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0)

            connection_text = f"Connected to {SERIAL_PORT} at {BAUD_RATE} baud"

        except serial.SerialException as e:

            connection_text = f"Not connected to {SERIAL_PORT} (keyboard only)"

            messagebox.showerror(
                "Serial Connection Error",
                f"Could not open {SERIAL_PORT}.\n\n"
                f"Change SERIAL_PORT at the top of the file.\n\n{e}",
            )

        tk.Label(root, text=connection_text).pack(padx=20, pady=(12, 6))

        main = tk.Frame(root)

        main.pack(padx=20, pady=(6, 16))

        self.build_road(main)

        self.build_panel(main)

        # Keyboard controls for testing without the board

        root.bind("<Left>", lambda e: self.game.press("left"))

        root.bind("<Right>", lambda e: self.game.press("right"))

        root.bind("<Down>", lambda e: self.game.press("freeze"))

        root.bind("<Up>", lambda e: self.game.press("revive"))

        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.tick()

    # ---------------------------------------------------------

    # Build the window

    # ---------------------------------------------------------

    def build_road(self, parent):

        total = ROWS + PREVIEW_ROWS

        size = self.CELL

        left = tk.Frame(parent)

        left.grid(row=0, column=0, padx=(0, 20))

        self.canvas = tk.Canvas(
            left, width=COLS * size, height=total * size, highlightthickness=0
        )

        self.canvas.pack()

        self.cell_bg = []

        self.cell_dot = []

        for r in range(total):

            bg_row = []

            dot_row = []

            for c in range(COLS):

                x = c * size

                y = r * size

                bg_row.append(
                    self.canvas.create_rectangle(
                        x, y, x + size, y + size, fill=ROAD_COLOR, outline="#555555"
                    )
                )

                dot_row.append(
                    self.canvas.create_oval(
                        x + 12,
                        y + 12,
                        x + size - 12,
                        y + size - 12,
                        fill=CAR_COLOR,
                        outline="",
                        state="hidden",
                    )
                )

            self.cell_bg.append(bg_row)

            self.cell_dot.append(dot_row)

        # Outline around the three lanes that are shown on the board

        self.canvas.create_rectangle(
            2,
            PREVIEW_ROWS * size + 1,
            COLS * size - 2,
            total * size - 2,
            outline="white",
            width=3,
        )

        tk.Label(left, text="White box = the 15 LEDs on the board").pack(pady=(6, 0))

    def build_panel(self, parent):

        panel = tk.Frame(parent)

        panel.grid(row=0, column=1, sticky="n")

        tk.Label(panel, text="Crossy Road", font=("Arial", 16, "bold")).pack(anchor="w")

        self.message_label = tk.Label(panel, text="", font=("Arial", 11))

        self.message_label.pack(anchor="w", pady=(2, 10))

        self.score_label = tk.Label(panel, font=("Arial", 12))

        self.score_label.pack(anchor="w")

        self.best_label = tk.Label(panel, font=("Arial", 12))

        self.best_label.pack(anchor="w")

        self.level_label = tk.Label(panel, font=("Arial", 12))

        self.level_label.pack(anchor="w")

        # Skills

        self.freeze_label = tk.Label(panel, font=("Arial", 12))

        self.freeze_label.pack(anchor="w", pady=(10, 0))

        self.revive_label = tk.Label(panel, font=("Arial", 12))

        self.revive_label.pack(anchor="w")

        # Difficulty and preview

        options = tk.Frame(panel)

        options.pack(anchor="w", pady=(12, 0))

        tk.Label(options, text="Difficulty:").grid(row=0, column=0)

        self.difficulty_var = tk.StringVar(value=self.game.difficulty)

        tk.OptionMenu(
            options,
            self.difficulty_var,
            *DIFFICULTY.keys(),
            command=self.set_difficulty,
        ).grid(row=0, column=1, padx=(6, 0))

        self.preview_var = tk.BooleanVar(value=False)

        tk.Checkbutton(
            panel, text="Show lanes ahead (easy mode)", variable=self.preview_var
        ).pack(anchor="w")

        # Start / pause

        controls = tk.Frame(panel)

        controls.pack(anchor="w", pady=(10, 0))

        tk.Button(
            controls, text="Start / Restart", width=14, command=self.game.start
        ).grid(row=0, column=0)

        tk.Button(controls, text="Pause", width=8, command=self.game.toggle_pause).grid(
            row=0, column=1, padx=(6, 0)
        )

        # Push-button states, in the same order as on the board

        tk.Label(panel, text="Push Buttons", font=("Arial", 11, "bold")).pack(
            anchor="w", pady=(14, 2)
        )

        self.button_canvas = tk.Canvas(
            panel, width=240, height=56, highlightthickness=0
        )

        self.button_canvas.pack(anchor="w")

        self.button_indicators = {}

        for i, action in enumerate(self.BUTTON_ORDER):

            x = 30 + i * 60

            self.button_indicators[action] = self.button_canvas.create_oval(
                x - 14, 4, x + 14, 32, fill="gray"
            )

            self.button_canvas.create_text(x, 45, text=action.upper())

        self.received_label = tk.Label(panel, text="Received: --")

        self.received_label.pack(anchor="w", pady=(6, 0))

        self.sent_label = tk.Label(panel, text="Last sent: --")

        self.sent_label.pack(anchor="w")

    def set_difficulty(self, name):

        self.game.difficulty = name

    # ---------------------------------------------------------

    # Main loop

    # ---------------------------------------------------------

    def tick(self):

        now = time.monotonic()

        dt = min(now - self.last_time, 0.1)

        self.last_time = now

        self.read_serial()

        self.game.update(dt)

        frame = self.game.led_frame(now)

        self.send_leds(frame, now)

        self.draw(frame)

        self.root.after(TICK_MS, self.tick)

    # ---------------------------------------------------------

    # Serial: read push-buttons

    # ---------------------------------------------------------

    def read_serial(self):

        if self.ser is None or not self.ser.is_open:

            return

        try:

            waiting = self.ser.in_waiting

            if waiting > 0:

                self.rx_buffer += self.ser.read(waiting)

        except (serial.SerialException, OSError):

            return

        if len(self.rx_buffer) > 4096:

            self.rx_buffer.clear()

        # Only handle complete lines.  A half-received number stays in

        # the buffer until the rest of it arrives.

        while b"\n" in self.rx_buffer:

            line, _, rest = self.rx_buffer.partition(b"\n")

            self.rx_buffer = bytearray(rest)

            values = line.decode("ascii", errors="ignore").split()

            if len(values) != 1:

                continue

            try:

                raw = int(values[0])

            except ValueError:

                continue

            self.handle_buttons(raw)

    def handle_buttons(self, raw):

        # Only the lowest 4 bits are push-buttons.

        bits = raw & 0x000F

        if BUTTON_PRESSED_VALUE == 0:

            bits = ~bits & 0x000F

        for action, bit in BUTTON_BITS.items():

            pressed = (bits >> bit) & 1

            # Act once when a button goes from released to pressed.

            if pressed and not self.held[action]:

                self.game.press(action)

            self.held[action] = pressed

        self.received_label.config(text=f"Received: {raw}")

    # ---------------------------------------------------------

    # Serial: send LEDs

    # ---------------------------------------------------------

    def send_leds(self, frame, now):

        if self.ser is None or not self.ser.is_open:

            return

        unchanged = frame == self.last_frame

        if unchanged and now - self.last_send_time < RESEND_S:

            return

        # Sends:

        #

        # 16bit number\r\n

        try:

            self.ser.write(f"{frame}\r\n".encode("ascii"))

        except serial.SerialException:

            return

        self.last_frame = frame

        self.last_send_time = now

    # ---------------------------------------------------------

    # Drawing

    # ---------------------------------------------------------

    def draw(self, frame):

        g = self.game

        total = ROWS + PREVIEW_ROWS

        live = g.state in ("playing", "paused")

        preview = self.preview_var.get()

        for canvas_row in range(total):

            lane_index = total - 1 - canvas_row

            lane = g.lanes[lane_index]

            on_board = lane_index < ROWS

            for col in range(COLS):

                dot = None

                if not on_board and not preview:

                    bg = FOG_COLOR

                elif on_board and not live:

                    # Menu / crash / game over: mirror the LED animation

                    bg = ROAD_COLOR

                    board_row = ROWS - 1 - lane_index

                    if (frame >> led_bit(board_row, col)) & 1:

                        dot = LED_COLOR

                else:

                    bg = ROAD_COLOR if any(lane) else GRASS_COLOR

                    if lane[col]:

                        dot = CAR_COLOR

                    if live and lane_index == 0 and col == g.player_col:

                        lit = (frame >> led_bit(ROWS - 1, col)) & 1

                        dot = PLAYER_COLOR if lit else None

                self.canvas.itemconfig(self.cell_bg[canvas_row][col], fill=bg)

                if dot is None:

                    self.canvas.itemconfig(
                        self.cell_dot[canvas_row][col], state="hidden"
                    )

                else:

                    self.canvas.itemconfig(
                        self.cell_dot[canvas_row][col], fill=dot, state="normal"
                    )

        # Text

        self.message_label.config(text=self.message())

        self.score_label.config(text=f"Score: {g.score}")

        self.best_label.config(text=f"Best ({g.difficulty}): {g.best[g.difficulty]}")

        self.level_label.config(text=f"Speed level: {g.level + 1}")

        self.freeze_label.config(text=f"Freezes left (LED16): {g.freezes_left}")

        self.revive_label.config(text=f"Revives left: {g.revives_left}")

        # Push-button indicators

        for action, item in self.button_indicators.items():

            self.button_canvas.itemconfig(
                item, fill="green" if self.held[action] else "gray"
            )

        self.sent_label.config(text=f"Last sent: {frame}")

    def message(self):

        g = self.game

        if g.state == "menu":

            return "Press any push-button to start"

        if g.state == "paused":

            return "Paused"

        if g.state == "revive":

            return f"Crash! Press REVIVE ({g.revive_timer:.1f} s)"

        if g.state == "crashed":

            return "Crash!"

        if g.state == "gameover":

            text = f"Game over. Score {g.score}."

            if g.new_best:

                text += " New best!"

            return text

        if g.freeze_timer > 0:

            return f"Time frozen ({g.freeze_timer:.1f} s)"

        if g.about_to_scroll():

            return "Get ready: check the lane above you"

        return "Dodge the cars"

    # ---------------------------------------------------------

    # Close program

    # ---------------------------------------------------------

    def close(self):

        if self.ser is not None and self.ser.is_open:

            try:

                # Turn every LED off before leaving

                self.ser.write(b"0\r\n")

                self.ser.flush()

            except serial.SerialException:

                pass

            self.ser.close()

        self.root.destroy()


# =============================================================

# Main

# =============================================================


if __name__ == "__main__":

    root = tk.Tk()

    app = CrossyRoadGUI(root)

    root.mainloop()
