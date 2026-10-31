"""GUI helpers: database context, number parsing, error handling, colours."""

from tkinter import messagebox
import contextlib

from meal_planner.db.connection import get_connection


RED, GREEN = "#e5484d", "#30a46c"      # readable on both the light and the dark theme


@contextlib.contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def number(text, name="value"):
    try:
        return float(str(text).replace(",", "."))
    except ValueError:
        raise ValueError(f"'{text}' is not a valid number ({name}).")


def safe(function):
    """Shows the error to the user instead of letting the window close."""
    def inner(self, *args):
        try:
            return function(self, *args)
        except Exception as e:
            self.msg(str(e), True)
            messagebox.showerror("Warning", str(e))
    return inner
