# Local vision model (free, on the rack server)

    curl -fsSL https://ollama.com/install.sh | sh     # installs and enables ollama.service
    ollama pull qwen2.5vl:7b                          # ~6 GB download, once

- GPU with 8 GB+ memory: a page takes a few seconds. CPU only: 30-90 s per page
  (it's only used for pages the rules can't read, so this is usually fine).
- In .env: LOCAL_VLM_URL=http://127.0.0.1:11434 and LOCAL_VLM_MODEL=qwen2.5vl:7b
- Then re-read files that were stored as unreadable before the model existed:

      .venv/bin/python -m scripts.retry_unreadable           # dry run
      .venv/bin/python -m scripts.retry_unreadable --apply
