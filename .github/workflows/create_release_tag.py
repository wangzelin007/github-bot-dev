import os
import re
import requests
import subprocess
import tempfile
import zipfile
from typing import List, Dict, Tuple, Optional
import sys

TARGET_FILE = "src/index.json"

def get_api_urls() -> Tuple[str, str]:
    """Generate GitHub API URLs based on GITHUB_REPOSITORY environment variable."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("Error: GITHUB_REPOSITORY environment variable is not set")
        print("This script is designed to run in GitHub Actions environment")
        sys.exit(1)

    base_url = f"https://api.github.com/repos/{repo}"
    return f"{base_url}/releases", f"{base_url}/git/tags"

def get_file_changes() -> List[str]:
    diff_output = subprocess.check_output(
        ["git", "diff", "HEAD^", "HEAD", "--", TARGET_FILE],
        text=True
    )

    added_lines = [
        line[1:].strip()
        for line in diff_output.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]

    return added_lines

def parse_filename(added_lines: List[str]) -> Optional[Dict]:
    """Parse filename and metadata from added lines"""
    filename = None
    metadata = {}
    download_url = None
    sha256_digest = None
    in_metadata = False

    for line in added_lines:
        line = line.strip()
        
        if '"filename":' in line:
            try:
                filename = line.split(":")[1].strip().strip('",')
            except IndexError:
                print(f"Error parsing line: {line}")
                
        elif '"downloadUrl":' in line:
            try:
                download_url = line.split(":")[1].strip().strip('",')
            except IndexError:
                print(f"Error parsing downloadUrl: {line}")
                
        elif '"sha256Digest":' in line:
            try:
                sha256_digest = line.split(":")[1].strip().strip('",')
            except IndexError:
                print(f"Error parsing sha256Digest: {line}")
                
        elif '"metadata":' in line:
            in_metadata = True
            continue
            
        elif in_metadata and line.startswith('"'):
            # Parse metadata key-value pairs
            try:
                key = line.split(":")[0].strip().strip('"')
                value = line.split(":")[1].strip().strip('",')
                metadata[key] = value
            except IndexError:
                print(f"Error parsing metadata line: {line}")
                
        elif line.startswith("}"):
            in_metadata = False

    if not filename:
        print("No filename found in added lines")
        return None

    if not download_url:
        print("No downloadUrl found in added lines")
        return None

    if not sha256_digest:
        print("No sha256Digest found in added lines")
        return None

    return {
        "filename": filename,
        "metadata": metadata,
        "downloadUrl": download_url,
        "sha256Digest": sha256_digest
    }

def generate_tag_and_title(filename: str) -> Tuple[str, str, str]:
    """Generate tag name and release title from filename"""
    match = re.match(r"^(.*?)[-_](\d+\.\d+\.\d+[a-z0-9]*)", filename)
    if not match:
        raise ValueError(f"Invalid filename format: {filename}")

    name = match.group(1).replace("_", "-")
    version = match.group(2)
    
    tag_name = f"{name}-{version}"
    release_title = f"{name} {version}"
    return tag_name, release_title, version

def check_tag_exists(url: str, tag_name: str, headers: Dict[str, str]) -> bool:
    response = requests.get(
        f"{url}/{tag_name}",
        headers=headers
    )
    return response.status_code == 200

def create_release(url: str, release_data: Dict[str, str], headers: Dict[str, str], wheel_url: str = None) -> None:
    try:
        # Create release
        response = requests.post(
            url,
            json=release_data,
            headers=headers
        )
        response.raise_for_status()
        print(f"Successfully created release for {release_data['tag_name']}")
        release_info = response.json()

        # Upload wheel file if URL is provided
        if wheel_url:
            print(f"Downloading wheel from {wheel_url}")
            wheel_response = requests.get(wheel_url)
            wheel_response.raise_for_status()
            
            upload_url = release_info["upload_url"].replace("{?name,label}", "")
            upload_headers = {
                "Authorization": headers["Authorization"],
                "Content-Type": "application/octet-stream"
            }
            params = {"name": os.path.basename(wheel_url)}
            
            print(f"Uploading wheel to {upload_url}")
            upload_response = requests.post(
                upload_url,
                headers=upload_headers,
                params=params,
                data=wheel_response.content
            )
            upload_response.raise_for_status()
            print(f"Successfully uploaded wheel file")

    except requests.exceptions.RequestException as e:
        print(f"\nError creating release for {release_data['tag_name']}")
        print(f"Error type: {type(e).__name__}")
        print(f"Error message: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response status code: {e.response.status_code}")
            print(f"Response body: {e.response.text}")
        raise

def generate_release_body(history_note: str, sha256_digest: str, filename: str) -> str:
    """Generate release body with history notes and wheel information"""
    return f"{history_note}\n\nSHA256 hashes of the release artifacts:\n```\n{sha256_digest} {filename}\n```\n"

def get_history_note(wheel_url: str, version: str) -> str:
    """Download wheel package and extract HISTORY.rst to find version notes"""
    try:
        # Download wheel file
        response = requests.get(wheel_url)
        response.raise_for_status()
        
        with tempfile.TemporaryFile() as temp_file:
            temp_file.write(response.content)
            temp_file.seek(0)
            
            # Open wheel as zip
            with zipfile.ZipFile(temp_file, 'r') as wheel:
                # Find DESCRIPTION.rst file
                history_files = [f for f in wheel.namelist() if f.endswith('DESCRIPTION.rst')]
                if not history_files:
                    return "No history notes found"
                
                history_content = wheel.read(history_files[0]).decode('utf-8')
                
                # Match any line starting with the version number
                version_pattern = rf"^{re.escape(version)}.*?\n\++\n(.*?)(?=^[\d.]+[a-z0-9].*?\n\++|\Z)"
                match = re.search(version_pattern, history_content, re.DOTALL | re.MULTILINE)
                
                if match:
                    return match.group(1).strip()
                    
                return "No history notes found for this version"
                
    except Exception as e:
        print(f"Error getting history notes: {e}")
        return "Failed to retrieve history notes"

def get_history_note_from_source(version: str, extension_name: str) -> str:
    """Get history notes from source code HISTORY.rst"""
    try:
        history_path = f"src/{extension_name}/HISTORY.rst"
        if not os.path.exists(history_path):
            return "No history notes found"

        with open(history_path, 'r', encoding='utf-8') as f:
            history_content = f.read()

        # Match any line starting with the version number
        version_pattern = rf"^{re.escape(version)}.*?\n\++\n(.*?)(?=^[\d.]+[a-z0-9].*?\n\++|\Z)"
        match = re.search(version_pattern, history_content, re.DOTALL | re.MULTILINE)
        
        if match:
            return match.group(1).strip()
            
        return "No history notes found in source code"
                
    except Exception as e:
        print(f"Error reading history from source: {e}")
        return "Failed to retrieve history notes"

def main():
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        print("Error: GITHUB_TOKEN environment variable is required")
        sys.exit(1)

    release_url, tag_url = get_api_urls()
    
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }

    try:
        added_lines = get_file_changes()
        if not added_lines:
            print("No changes found in index.json")
            return

        file_info = parse_filename(added_lines)
        if not file_info:
            print("No filename found in changes")
            return

        try:
            filename = file_info["filename"]
            tag_name, release_title, version = generate_tag_and_title(filename)
            
            # Get history notes from wheel package
            print(f"Getting history notes from wheel package...")
            history_note = get_history_note(file_info["downloadUrl"], version)
            
            # If no notes found in wheel, try source code
            if "No history notes found" in history_note:
                print(f"No history notes found in wheel, trying source code...")
                extension_name = re.match(r"^(.*?)[-_]\d+\.\d+\.\d+", filename).group(1)
                history_note = get_history_note_from_source(version, extension_name)

            # Generate release body
            release_body = generate_release_body(history_note, file_info["sha256Digest"], filename)
            
            commit_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                text=True
            ).strip()

            if check_tag_exists(tag_url, tag_name, headers):
                print(f"Tag {tag_name} already exists, skipping...")
                return

            release_data = {
                "tag_name": tag_name,
                "target_commitish": commit_sha,
                "name": release_title,
                "body": release_body
            }

            print(f"\nCreating release with data:")
            print(f"Tag name: {tag_name}")
            print(f"Release title: {release_title}")
            print(f"Target commit: {commit_sha}")
            print(f"Body preview: {release_body[:200]}...")

            create_release(release_url, release_data, headers, file_info["downloadUrl"])

        except ValueError as e:
            print(f"Error generating tag for filename {filename}: {e}")
            return

    except Exception as e:
        print(f"Unexpected error: {e}")
        raise

if __name__ == "__main__":
    main()
