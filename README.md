# TypeFuzz: Type Coverage Directed JavaScript Engine Fuzzing

https://www.ndss-symposium.org/wp-content/uploads/fuzzing2026-5.pdf

## Build

```bash
docker build -f eval/Dockerfile -t typefuzz-eval .
```

## Run

```bash
python3 fuzzing_run.py \
    --feedback-mode type \
    --num-workers 5 \
    --cores-per-worker 10 \
    --start-core 0 \
    --duration 72
```

Feedback modes: `type` (type coverage), `code` (edge coverage), `hybrid` (both).
