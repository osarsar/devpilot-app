"""DevPilot — Cloudflare R2 storage operations (S3-compatible via boto3)."""

import os
import mimetypes
from pathlib import Path
from fnmatch import fnmatch

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError, EndpointConnectionError


def r2_get_client(account_id, access_key, secret_key):
    """Create a boto3 S3 client configured for Cloudflare R2."""
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "adaptive"},
        ),
        region_name="auto",
    )


def r2_test_connection(account_id, access_key, secret_key):
    """Test R2 connection by listing buckets."""
    if not account_id or not access_key or not secret_key:
        return {"connected": False, "message": "Identifiants R2 manquants"}
    try:
        client = r2_get_client(account_id, access_key, secret_key)
        resp = client.list_buckets()
        buckets = [b["Name"] for b in resp.get("Buckets", [])]
        return {
            "connected": True,
            "buckets": buckets,
            "bucket_count": len(buckets),
        }
    except NoCredentialsError:
        return {"connected": False, "message": "Identifiants invalides"}
    except EndpointConnectionError:
        return {"connected": False, "message": "Impossible de se connecter a R2 (verifier account_id)"}
    except ClientError as e:
        return {"connected": False, "message": e.response["Error"].get("Message", str(e))}
    except Exception as e:
        return {"connected": False, "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# BUCKET OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

def r2_list_buckets(client):
    """List all R2 buckets."""
    try:
        resp = client.list_buckets()
        return [{
            "name": b["Name"],
            "created": b.get("CreationDate", "").isoformat() if b.get("CreationDate") else "",
        } for b in resp.get("Buckets", [])]
    except Exception as e:
        return []


def r2_create_bucket(client, name):
    """Create a new R2 bucket."""
    try:
        client.create_bucket(Bucket=name)
        return {"success": True, "message": f"Bucket '{name}' cree"}
    except ClientError as e:
        return {"success": False, "message": e.response["Error"].get("Message", str(e))}
    except Exception as e:
        return {"success": False, "message": str(e)}


def r2_delete_bucket(client, name):
    """Delete an empty R2 bucket."""
    try:
        client.delete_bucket(Bucket=name)
        return {"success": True, "message": f"Bucket '{name}' supprime"}
    except ClientError as e:
        return {"success": False, "message": e.response["Error"].get("Message", str(e))}


# ═══════════════════════════════════════════════════════════════════════════
# OBJECT OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

def r2_list_objects(client, bucket, prefix="", max_keys=200):
    """List objects in a bucket with optional prefix."""
    try:
        params = {"Bucket": bucket, "MaxKeys": max_keys}
        if prefix:
            params["Prefix"] = prefix

        objects = []
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(**params, PaginationConfig={"MaxItems": max_keys})

        for page in pages:
            for obj in page.get("Contents", []):
                objects.append({
                    "key": obj["Key"],
                    "size": obj.get("Size", 0),
                    "last_modified": obj.get("LastModified", "").isoformat() if obj.get("LastModified") else "",
                    "etag": obj.get("ETag", "").strip('"'),
                })
        return objects
    except ClientError as e:
        return []
    except Exception:
        return []


def r2_upload_file(client, bucket, local_path, remote_key, content_type=""):
    """Upload a single file to R2."""
    try:
        if not os.path.isfile(local_path):
            return {"success": False, "message": f"Fichier introuvable: {local_path}"}

        if not content_type:
            content_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"

        file_size = os.path.getsize(local_path)
        extra_args = {"ContentType": content_type}

        client.upload_file(local_path, bucket, remote_key, ExtraArgs=extra_args)
        return {
            "success": True,
            "key": remote_key,
            "size": file_size,
            "content_type": content_type,
        }
    except ClientError as e:
        return {"success": False, "message": e.response["Error"].get("Message", str(e))}
    except Exception as e:
        return {"success": False, "message": str(e)}


def r2_upload_directory(client, bucket, local_dir, remote_prefix, exclude_patterns=None):
    """Upload a directory recursively to R2."""
    if exclude_patterns is None:
        exclude_patterns = []

    results = {"uploaded": 0, "failed": 0, "total_bytes": 0, "errors": []}

    local_dir = str(local_dir)
    for root, dirs, files in os.walk(local_dir):
        # Skip hidden dirs and common build dirs
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                   ("node_modules", "__pycache__", ".git", "venv", ".venv")]

        for fname in files:
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, local_dir)

            # Check exclude patterns
            if any(fnmatch(rel_path, pat) or fnmatch(fname, pat) for pat in exclude_patterns):
                continue

            remote_key = f"{remote_prefix}/{rel_path}" if remote_prefix else rel_path
            remote_key = remote_key.replace("\\", "/")

            result = r2_upload_file(client, bucket, fpath, remote_key)
            if result.get("success"):
                results["uploaded"] += 1
                results["total_bytes"] += result.get("size", 0)
            else:
                results["failed"] += 1
                results["errors"].append(f"{rel_path}: {result.get('message', '')}")

    return results


def r2_download_file(client, bucket, remote_key, local_path):
    """Download a file from R2."""
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        client.download_file(bucket, remote_key, local_path)
        return {"success": True, "key": remote_key, "local_path": local_path, "size": os.path.getsize(local_path)}
    except ClientError as e:
        return {"success": False, "message": e.response["Error"].get("Message", str(e))}
    except Exception as e:
        return {"success": False, "message": str(e)}


def r2_download_directory(client, bucket, remote_prefix, local_dir):
    """Download all objects with a given prefix to a local directory."""
    objects = r2_list_objects(client, bucket, prefix=remote_prefix, max_keys=1000)
    results = {"downloaded": 0, "failed": 0, "total_bytes": 0, "errors": []}

    for obj in objects:
        key = obj["key"]
        # Strip prefix to get relative path
        rel_path = key[len(remote_prefix):].lstrip("/")
        if not rel_path:
            continue
        local_path = os.path.join(local_dir, rel_path)

        result = r2_download_file(client, bucket, key, local_path)
        if result.get("success"):
            results["downloaded"] += 1
            results["total_bytes"] += result.get("size", 0)
        else:
            results["failed"] += 1
            results["errors"].append(f"{key}: {result.get('message', '')}")

    return results


def r2_delete_object(client, bucket, key):
    """Delete a single object from R2."""
    try:
        client.delete_object(Bucket=bucket, Key=key)
        return {"success": True, "message": f"'{key}' supprime"}
    except ClientError as e:
        return {"success": False, "message": e.response["Error"].get("Message", str(e))}


def r2_delete_prefix(client, bucket, prefix):
    """Delete all objects with a given prefix."""
    objects = r2_list_objects(client, bucket, prefix=prefix, max_keys=1000)
    deleted = 0
    for obj in objects:
        try:
            client.delete_object(Bucket=bucket, Key=obj["key"])
            deleted += 1
        except Exception:
            pass
    return {"success": True, "deleted": deleted}


def r2_get_object_info(client, bucket, key):
    """Get metadata for a single object."""
    try:
        resp = client.head_object(Bucket=bucket, Key=key)
        return {
            "success": True,
            "key": key,
            "size": resp.get("ContentLength", 0),
            "content_type": resp.get("ContentType", ""),
            "etag": resp.get("ETag", "").strip('"'),
            "last_modified": resp.get("LastModified", "").isoformat() if resp.get("LastModified") else "",
        }
    except ClientError:
        return {"success": False, "message": "Objet introuvable"}


def r2_get_presigned_url(client, bucket, key, expires=3600):
    """Generate a presigned URL for temporary access."""
    try:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )
        return {"success": True, "url": url, "expires_in": expires}
    except Exception as e:
        return {"success": False, "message": str(e)}


def r2_get_bucket_size(client, bucket, prefix=""):
    """Calculate total size of objects in a bucket/prefix."""
    objects = r2_list_objects(client, bucket, prefix=prefix, max_keys=10000)
    total = sum(obj.get("size", 0) for obj in objects)
    return {"count": len(objects), "total_size": total}
