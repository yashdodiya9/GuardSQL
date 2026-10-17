from fastapi import FastAPI

from guardsql import __version__

app = FastAPI(
    title="GuardSQL",
    version=__version__,
    description="Text-to-SQL with guardrails and hallucination detection.",
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
