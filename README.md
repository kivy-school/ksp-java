# ksp-java
Easy way to bundle java files in python packages.

A PEP 517 build backend (wrapping hatchling) that includes Java source folders in built distributions under
`.java/` (for wheels this becomes `site-packages/.java`).

Use `tool.ksp-java.java-paths` in `pyproject.toml` to configure the folders to bundle:

```toml
[build-system]
requires = ["ksp-java"]
build-backend = "ksp_java"

[tool.ksp-java]
java-paths = [
    "root/path/to/java"
]
```
