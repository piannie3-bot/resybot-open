import click
import os

@click.command()
def start():
    import resygrabber
    resygrabber.menu()

if __name__ == "__main__":
    start()
