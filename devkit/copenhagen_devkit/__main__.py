"""Development kit CLI: seed example data, run the simulator, run the guided demo.

Not part of the shipped product. Run with ``uv run python -m copenhagen_devkit``.
"""

import asyncio

import typer

app = typer.Typer(help="Copenhagen development kit (never deployed)")


@app.command("seed")
def seed() -> None:
    from copenhagen.cli import output, service
    from copenhagen_devkit.seed import seed as run

    run(service())
    output({"seeded": True})


@app.command("demo")
def demo() -> None:
    from copenhagen_devkit.demo import demo as run

    asyncio.run(run())


@app.command("mockworld")
def mockworld(port: int = 8010) -> None:
    import uvicorn

    uvicorn.run("copenhagen_devkit.mockworld:create_app", factory=True, host="127.0.0.1", port=port)


if __name__ == "__main__":
    app()
