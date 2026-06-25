from textual.widgets import RichLog


class ChatLog(RichLog):
    """A scrolling transcript: chat bubbles on the user side, directional lines on
    the wire side."""

    def __init__(self, **kwargs):
        super().__init__(markup=True, wrap=True, auto_scroll=True, **kwargs)

    def say(self, who: str, text: str, color: str = "cyan") -> None:
        self.write(f"[{color}]{who}[/]: {text}")

    def wire(self, outbound: bool, text: str, note: str | None = None) -> None:
        arrow = "[green]→[/]" if outbound else "[yellow]←[/]"
        line = f"{arrow} {text}"
        if note:
            line += f"  [dim]({note})[/]"
        self.write(line)
