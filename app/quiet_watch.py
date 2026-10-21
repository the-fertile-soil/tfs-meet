"""Freeze transcription and the summarizer while any meeting is running; thaw them when it is quiet."""
import subprocess
import quiet

UNIT = "tfs-process.service"
CONTAINER = "ollama-ollama-1"


def sh(*a):
    return subprocess.run(a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def ollama_paused() -> bool:
    return sh("docker", "inspect", "-f", "{{.State.Paused}}", CONTAINER).stdout.strip() == "true"


def main():
    if quiet.meetings_live():
        if not ollama_paused():
            sh("docker", "pause", CONTAINER)
        if sh("systemctl", "is-active", UNIT).stdout.strip() == "active":
            sh("systemctl", "freeze", UNIT)
    else:
        sh("systemctl", "thaw", UNIT)
        if ollama_paused():
            sh("docker", "unpause", CONTAINER)


if __name__ == "__main__":
    main()
