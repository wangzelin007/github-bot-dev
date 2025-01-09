import os
import re
import requests
import subprocess
import tempfile
import zipfile
from typing import List, Dict, Tuple, Optional
import sys
import json

repo = os.environ.get("GITHUB_REPOSITORY")
TARGET_FILE = "src/index.json"
base_url = f"https://api.github.com/repos/{repo}"
github_token = os.environ.get("GITHUB_TOKEN")
if not github_token:
    print("Error: GITHUB_TOKEN environment variable is required")
    sys.exit(1)
headers = {
    "Authorization": f"token {github_token}",
    "Accept": "application/vnd.github.v3+json"
}


def get_file_diff() -> List[str]:
    """Get added lines from git diff"""
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


def parse_filenames(added_lines: List[str]) -> List[str]:
    """Parse filenames from added lines"""
    filenames = []
    for line in added_lines:
        if '"filename":' in line:
            try:
                filename = line.split(":")[1].strip().strip('",')
                filenames.append(filename)
            except IndexError:
                print(f"Error parsing line: {line}")
    return filenames


def parse_sha256_digest(added_lines: List[str]) -> str:
    for line in added_lines:
        if "sha256Digest" in line:
            try:
                sha_match = re.search(r'"sha256Digest"\s*:\s*"([a-fA-F0-9]{64})"', line)
                if sha_match:
                    sha = sha_match.group(1)
                    print(f"get sha256Digest change: {sha}")
                    return sha
            except IndexError:
                print(f"Error parsing line: {line}")
    return None


def get_file_info_by_sha(sha: str) -> Tuple[str, str]:
    try:
        with open(TARGET_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        for ext_name, versions in data["extensions"].items():
            for version in versions:
                if version["sha256Digest"] == sha:
                    return version["filename"], version["downloadUrl"]
                    
        return None, None
        
    except FileNotFoundError:
        print(f"Error: {TARGET_FILE} not found")
        raise
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON format in {TARGET_FILE}")
        raise


def get_release_info(tag_name: str) -> Tuple[Optional[int], Optional[int]]:
    try:
        url = f"{base_url}/releases/tags/{tag_name}"

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()

        release_id = data.get("id")
        release_body = data.get("body")
        assets = data.get("assets", [])
        asset_id = assets[0].get("id") if assets else None

        return release_id, release_body, asset_id
    except requests.RequestException as e:
        print(f"API request failed: {e}")
        return None, None, None
    except (KeyError, IndexError) as e:
        print(f"Failed to parse response data: {e}")
        return None, None, None


def update_release_body(release_id: int, commit_sha: str, old_body: str) -> bool:
    try:
        url = f"{base_url}/releases/{release_id}"

        sha_pattern = r'[a-fA-F0-9]{64}'
        new_body = old_body

        found_shas = re.finditer(sha_pattern, old_body)
        for match in found_shas:
            old_sha = match.group()

            if old_sha != commit_sha:
                new_body = new_body.replace(old_sha, commit_sha)
                break

        payload = {
            "target_commitish": commit_sha,
            "body": new_body
        }

        response = requests.patch(url, json=payload, headers=headers)
        response.raise_for_status()

        return True
    except requests.RequestException as e:
        print(f"Failed to update release: {e}")
        if hasattr(e.response, 'text'):
            print(f"Response: {e.response.text}")
        return False
    except Exception as e:
        print(f"Unexpected error: {e}")
        return False


def update_release_asset(wheel_url: str, asset_id: int) -> bool:
    try:
        print(f"Downloading wheel from {wheel_url}")
        wheel_response = requests.get(wheel_url)
        wheel_response.raise_for_status()

        delete_url = f"{base_url}/releases/assets/{asset_id}"
        delete_response = requests.delete(delete_url, headers=headers)
        delete_response.raise_for_status()
        print("Successfully deleted old asset")

        release_url = f"{base_url}/releases/assets/{asset_id}"
        release_response = requests.get(release_url, headers=headers)
        release_response.raise_for_status()
        release_data = release_response.json()
        upload_url = release_data["upload_url"].replace("{?name,label}", "")

        upload_headers = headers.copy()
        upload_headers["Content-Type"] = "application/octet-stream"
        params = {"name": os.path.basename(wheel_url)}

        print(f"Uploading new wheel to {upload_url}")
        upload_response = requests.post(
            upload_url,
            headers=upload_headers,
            params=params,
            data=wheel_response.content
        )
        upload_response.raise_for_status()
        print("Successfully updated wheel file")
        return True

    except requests.RequestException as e:
        print(f"Failed to update release asset: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}")
        return False
    except Exception as e:
        print(f"Unexpected error: {e}")
        return False


def get_extension_info(filename: str) -> Optional[Dict]:
    """Get extension information from index.json"""
    try:
        with open(TARGET_FILE, 'r') as f:
            index_data = json.load(f)

        # Search for the extension entry with matching filename
        for ext_name, versions in index_data.get("extensions", {}).items():
            for version in versions:
                if version.get("filename") == filename:
                    return version

        print(f"Extension {filename} not found in index.json")
        return None

    except Exception as e:
        print(f"Error reading index.json: {e}")
        return None


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


def check_tag_exists(tag_name: str) -> bool:
    url = f"{base_url}/tags/{tag_name}",
    response = requests.get(
        url,
        headers=headers
    )
    return response.status_code == 200


def create_release(release_data: Dict[str, str], wheel_url: str = None) -> None:
    try:
        url = f"{base_url}/releases"
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
            params = {"name": os.path.basename(wheel_url)}

            print(f"Uploading wheel to {upload_url}")
            upload_response = requests.post(
                upload_url,
                headers=headers,
                params=params,
                data=wheel_response.content
            )
            upload_response.raise_for_status()
            print("Successfully uploaded wheel file")

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
                version_pattern = rf"^{re.escape(version)}.*?\n(?:[-=+~]+\n)?(.*?)(?=^[\d.]+[a-z0-9].*?(?:\n[-=+~]+)?|\Z)"
                match = re.search(version_pattern, history_content, re.DOTALL | re.MULTILINE)

                if match:
                    return match.group(1).strip()

                return "No history notes found for this version"

    except Exception as e:
        print(f"Error getting history notes: {e}")
        return "No history notes found for this version"


def get_history_note_from_source(version: str, extension_name: str) -> str:
    """Get history notes from source code HISTORY.rst"""
    try:
        history_path = f"src/{extension_name}/HISTORY.rst"
        if not os.path.exists(history_path):
            return "No history notes found"

        with open(history_path, 'r', encoding='utf-8') as f:
            history_content = f.read()

        # Match any line starting with the version number
        version_pattern = rf"^{re.escape(version)}.*?\n(?:[-=+~]+\n)?(.*?)(?=^[\d.]+[a-z0-9].*?(?:\n[-=+~]+)?|\Z)"
        match = re.search(version_pattern, history_content, re.DOTALL | re.MULTILINE)

        if match:
            return match.group(1).strip()

        return "No history notes found in source code"

    except Exception as e:
        print(f"Error reading history from source: {e}")
        return "No history notes found for this version"


def main():
    try:
        # Get added lines from git diff
        added_lines = get_file_diff()
        if not added_lines:
            print("No changes found in index.json")
            return

        # Parse filenames from added lines
        filenames = parse_filenames(added_lines)
        commit_sha = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    text=True
                ).strip()
        if not filenames:
            print("No filenames found in changes")
            sha = parse_sha256_digest(added_lines)
            if not sha:
                print("No sha256Digest found in changes")
                return
            else:
                filename, wheel_url = get_file_info_by_sha(sha)
                tag_name, release_title, version = generate_tag_and_title(filename)
                release_id, release_body, asset_id = get_release_info(tag_name)
                update_release_body(release_id, commit_sha, release_body)
                update_release_asset(wheel_url, asset_id)

        print(f"Found {len(filenames)} files to process")
        # Process each filename
        for filename in filenames:
            print(f"\nProcessing {filename}...")

            # Get extension info from index.json
            extension_info = get_extension_info(filename)
            if not extension_info:
                print(f"Could not get extension information for {filename}, skipping...")
                continue

            try:
                tag_name, release_title, version = generate_tag_and_title(filename)
                # Check if tag already exists
                if check_tag_exists(tag_name):
                    print(f"Tag {tag_name} already exists, skipping...")
                    continue

                # Try to get history notes from source code first
                print(f"Getting history notes from source code...")
                extension_name = re.match(r"^(.*?)[-_]\d+\.\d+\.\d+", filename).group(1)
                history_note = get_history_note_from_source(version, extension_name)

                # If no notes found in source code, try wheel package
                if "No history notes found" in history_note:
                    print(f"No history notes found in source code, trying wheel package...")
                    history_note = get_history_note(extension_info["downloadUrl"], version)

                    # If still no notes found, use default release note
                    if "No history notes found" in history_note:
                        print(f"No history notes found in wheel package, using default release note...")
                        history_note = f"Release {extension_name} {version}"

                # Generate release body
                release_body = generate_release_body(history_note, extension_info["sha256Digest"], filename)

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

                create_release(release_data, extension_info["downloadUrl"])

            except ValueError as e:
                print(f"Error generating tag for filename {filename}: {e}")
                continue
            except Exception as e:
                print(f"Unexpected error processing {filename}: {e}")
                continue

    except Exception as e:
        print(f"Unexpected error: {e}")
        raise


if __name__ == "__main__":
    main()
