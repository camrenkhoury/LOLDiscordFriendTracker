import tkinter as tk
from tkinter import ttk

# IMPORT FROM YOUR BOT
from bot import classify_game, summarize_games

# -----------------------
# Fake game generator
# -----------------------

def make_game(
    win,
    team_death_burden=0,
    death_outliers=0,
    team_collapse=0,
    afk_penalty=0,
    low_damage=0,
    vision_grief=0,
    prpb=0,
    clean_early=0,
    objective_disparity=0,
    hard_carry=0,
):
    return {
        "win": win,
        "components": {
            "team_death_burden": team_death_burden,
            "death_outliers": death_outliers,
            "team_collapse": team_collapse,
            "afk_penalty": afk_penalty,
            "low_damage_grief": low_damage,
            "vision_grief": vision_grief,
            "player_relative_bonus": prpb,
            "clean_early_bonus": clean_early,
            "objective_disparity": objective_disparity,
            "hard_carry_bonus": hard_carry,
        }
    }

# -----------------------
# GUI
# -----------------------

root = tk.Tk()
root.title("Grief Tracker Simulator")

inputs = {}

FIELDS = [
    ("Team Death Burden", "team_death_burden"),
    ("Death Outliers", "death_outliers"),
    ("Team Collapse", "team_collapse"),
    ("AFK Penalty", "afk_penalty"),
    ("Low Damage", "low_damage"),
    ("Vision Grief", "vision_grief"),
    ("Player Survival Bonus", "prpb"),
    ("Clean Early Bonus", "clean_early"),
    ("Objective Disparity", "objective_disparity"),
    ("Hard Carry Bonus", "hard_carry"),
]

row = 0
for label, key in FIELDS:
    ttk.Label(root, text=label).grid(row=row, column=0, sticky="w")
    entry = ttk.Entry(root, width=10)
    entry.insert(0, "0")
    entry.grid(row=row, column=1)
    inputs[key] = entry
    row += 1

win_var = tk.BooleanVar(value=True)
ttk.Checkbutton(root, text="Win", variable=win_var).grid(row=row, column=0, sticky="w")
row += 1

output = tk.Text(root, height=12, width=60)
output.grid(row=row, column=0, columnspan=2, pady=10)

# -----------------------
# Simulation
# -----------------------

def run_sim():
    games = []

    for _ in range(10):
        games.append(
            make_game(
                win=win_var.get(),
                team_death_burden=float(inputs["team_death_burden"].get()),
                death_outliers=float(inputs["death_outliers"].get()),
                team_collapse=float(inputs["team_collapse"].get()),
                afk_penalty=float(inputs["afk_penalty"].get()),
                low_damage=float(inputs["low_damage"].get()),
                vision_grief=float(inputs["vision_grief"].get()),
                prpb=float(inputs["prpb"].get()),
                clean_early=float(inputs["clean_early"].get()),
                objective_disparity=float(inputs["objective_disparity"].get()),
                hard_carry=float(inputs["hard_carry"].get()),
            )
        )

    summary = summarize_games(games)

    output.delete("1.0", tk.END)
    output.insert(tk.END, "RESULTS (10 identical games)\n\n")

    for k, v in summary.items():
        if v:
            output.insert(tk.END, f"{k}: {v}\n")

    output.insert(tk.END, "\nPer-game classification:\n")
    label, emoji = classify_game(games[0])
    output.insert(tk.END, f"{emoji} {label}\n")

ttk.Button(root, text="Run Simulation", command=run_sim).grid(row=row+1, column=0)

root.mainloop()
