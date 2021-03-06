#!/usr/bin/python
"""Release a new Docker image and helm package.

This script should be run from the root directory of the repo.
"""

import argparse
import glob
from google.cloud import storage
import logging
import json
import os
import tarfile
import tempfile
from py import util
import yaml

REPO_ORG = "jlewi"
REPO_NAME = "mlkube.io"

RESULTS_BUCKET = "mlkube-testing-results"
JOB_NAME = "mlkube-build-postsubmit"

def get_latest_green_presubmit(gcs_client):
  bucket = gcs_client.get_bucket(RESULTS_BUCKET)
  latest_results = os.path.join(JOB_NAME)
  blob = bucket.blob(os.path.join(JOB_NAME, "latest_green.json"))
  contents = blob.download_as_string()

  results = json.loads(contents)

  if results.get("status", "").lower() != "passing":
    raise ValueError("latest results aren't green.")

  return results.get("sha", "")


def update_values(values_file, image):
  """Update the values file for the helm package to use the new image."""

  # We want to preserve comments so we don't use the yaml library.
  with open(values_file) as hf:
    lines = hf.readlines()

  with open(values_file, "w") as hf:
    for l in lines:
      if l.startswith("image:"):
        hf.write("image: {0}\n".format(image))
      else:
        hf.write(l)

def update_chart(chart_file, version):
  """Append the version number to the version number in chart.yaml"""
  with open(chart_file) as hf:
    info = yaml.load(hf)
  info["version"] += "-" + version
  info["appVersion"] += "-" + version

  with open(chart_file, "w") as hf:
    yaml.dump(info, hf)

if __name__ == "__main__":
  logging.getLogger().setLevel(logging.INFO)
  parser = argparse.ArgumentParser(
      description="Release artifacts for TfJob.")

  parser.add_argument(
      "--releases_bucket",
      default="tf-on-k8s-dogfood-releases",
      type=str,
      help="The bucket to publish releases to.")

  # TODO(jlewi): Should pass along unknown arguments to build and push.
  args, _ = parser.parse_known_args()

  gcs_client = storage.Client()
  sha = get_latest_green_presubmit(gcs_client)

  src_dir = tempfile.mkdtemp(prefix="tmpTfJobSrc")
  logging.info("src_dir: %s", src_dir)

  sha = util.clone_repo(src_dir, util.MASTER_REPO_OWNER, util.MASTER_REPO_NAME,
                        sha)

  # TODO(jlewi): We should check if we've already done a push. We could
  # check if the .tar.gz for the helm package exists.
  build_info_file = os.path.join(src_dir, "build_info.yaml")
  util.run([os.path.join(src_dir, "images", "tf_operator", "build_and_push.py"),
            "--output=" + build_info_file], cwd=src_dir)

  with open(build_info_file) as hf:
    build_info = yaml.load(hf)

  version = build_info["image"].split(":")[-1]
  values_file = os.path.join(src_dir, "tf-job-operator-chart", "values.yaml")
  update_values(values_file, build_info["image"])

  chart_file = os.path.join(src_dir, "tf-job-operator-chart", "Chart.yaml")
  update_chart(chart_file, version)

  util.run(["helm", "package", "./tf-job-operator-chart"], cwd=src_dir)

  matches = glob.glob(os.path.join(src_dir, "tf-job-operator-chart*.tgz"))

  if len(matches) != 1:
    raise ValueError("Expected 1 chart archive to match but found {0}".format(matches))

  chart_archive = matches[0]

  release_path = version

  bucket = gcs_client.get_bucket(args.releases_bucket)

  targets = [
    os.path.join(release_path, os.path.basename(chart_archive)),
    "latest/tf-job-operator-chart-latest.tgz",
  ]

  for t in targets:
    blob = bucket.blob(t)
    gcs_path = util.to_gcs_uri(args.releases_bucket, t)
    if blob.exists() and not t.startswith("latest"):
      logging.warn("%s already exists", gcs_path)
      continue
    logging.info("Uploading %s to %s.", chart_archive, gcs_path)
    blob.upload_from_filename(chart_archive)