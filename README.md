# halite-api

The API is stateless with respect to uploaded business files. The web app stores
context files in browser IndexedDB and posts them to the analysis endpoints when
needed. Dataset and upload compatibility routes keep data in process memory only,
so no SQLite file or writable persistent volume is required for deployment.
