# notes_app

## Tech Stack
- **frontend**: React
- **backend**: FastAPI

## API Endpoints
- `GET /api/health` — Health check endpoint.
- `POST /api/notes` — Create a new note.
- `GET /api/notes` — Get all notes.
- `PUT /api/notes/{note_id}` — Update an existing note.
- `DELETE /api/notes/{note_id}` — Delete a note.

## Frontend Components
- App
- NoteList
- NoteForm

## Folder Structure
```json
{
  "frontend": {
    "src": {
      "App.jsx": null,
      "components": {
        "NoteList.jsx": null,
        "NoteForm.jsx": null
      }
    },
    "public": {
      "index.html": null
    }
  },
  "backend": {
    "main.py": null,
    "routes": {
      "notes.py": null
    }
  },
  "docker-compose.yml": null
}
```
