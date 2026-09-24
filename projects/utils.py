import zipfile
import io
import re
from django.http import FileResponse


def safe_name(name):
    return re.sub(r"[^\w\- ]", "_", name)


def build_project_zip(project):
    """Project ki har file (brief + sab module files) ko ek zip mein
    memory ke andar bundle karke FileResponse return karta hai."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if project.brief:
            zf.write(project.brief.path, arcname=f"brief/{safe_name(project.brief.name.split('/')[-1])}")

        for module in project.modules.all():
            for f in module.files.all():
                arcname = f"{safe_name(module.name)}/{safe_name(f.original_name)}"
                zf.write(f.file.path, arcname=arcname)

    buffer.seek(0)
    return FileResponse(buffer, as_attachment=True, filename=f"{safe_name(project.name)}-files.zip")
