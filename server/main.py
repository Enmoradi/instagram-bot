from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import instaloader
import tempfile
import os

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Instagram Downloader API"}

@app.post("/download")
async def download_instagram(url: str):
    if "instagram.com/" not in url:
        raise HTTPException(status_code=400, detail="Invalid Instagram URL")

    L = instaloader.Instaloader(dirname_pattern="", save_metadata=False, quiet=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        shortcode = url.rstrip("/").split("/")[-1]
        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
        except Exception:
            raise HTTPException(status_code=404, detail="Post not found or private")

        L.download_post(post, target=tmpdir)

        for root, dirs, files in os.walk(tmpdir):
            for f in files:
                if f.endswith((".mp4", ".jpg", ".png")):
                    path = os.path.join(root, f)
                    mime = "video/mp4" if f.endswith(".mp4") else "image/jpeg"
                    return StreamingResponse(open(path, "rb"), media_type=mime, headers={"Content-Disposition": f"attachment; filename={f}"})

    raise HTTPException(status_code=500, detail="Download failed")
