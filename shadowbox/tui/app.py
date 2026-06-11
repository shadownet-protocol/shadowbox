from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, RichLog

from shadowbox import crypto
from shadowbox.config import Settings, load_config
from shadowbox.init import initialize


class ShadowboxApp(App):
    TITLE = "shadowbox"
    BINDINGS = [("q", "quit", "quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        settings = Settings()
        if not settings.initialized:
            log.write("[b]first run — initializing[/b]")
            for line in initialize(settings):
                log.write(line)
            return
        log.write(f"home: {settings.home_dir}")
        for p in load_config(settings).personas:
            key = crypto.load_key(settings.keys_dir / f"{p.name}.pem")
            log.write(
                f"[b]{p.name}[/b]  shadow://key:{crypto.public_multibase(key)}"
                f"@localhost:{p.port}"
            )


def main() -> None:
    ShadowboxApp().run()


if __name__ == "__main__":
    main()
