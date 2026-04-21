#!/usr/bin/env bash
set -euo pipefail

# -----------------------------
# Load .env if present
# -----------------------------
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# -----------------------------
# Required AWS vars
# -----------------------------
: "${AWS_DEFAULT_REGION:?Set AWS_DEFAULT_REGION in .env}"
: "${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID in .env}"

# Optional vars with defaults
ECR_REPO="${ECR_REPO:-portal}"
APP_RUNNER_SERVICE="${APP_RUNNER_SERVICE:-portal-service}"
APP_DIR="${APP_DIR:-./app}"

IMAGE_URI="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$ECR_REPO:latest"

echo "Region:   $AWS_DEFAULT_REGION"
echo "Account:  $AWS_ACCOUNT_ID"
echo "Repo:     $ECR_REPO"
echo "Service:  $APP_RUNNER_SERVICE"
echo "App dir:  $APP_DIR"
echo "Image:    $IMAGE_URI"

# -----------------------------
# Validate layout
# -----------------------------
if [ ! -d "$APP_DIR" ]; then
  echo "ERROR: App directory '$APP_DIR' not found."
  exit 1
fi

if [ ! -f "$APP_DIR/requirements.txt" ]; then
  echo "ERROR: '$APP_DIR/requirements.txt' not found."
  exit 1
fi

if [ -f "./Dockerfile" ]; then
  DOCKERFILE_SOURCE="./Dockerfile"
elif [ -f "$APP_DIR/Dockerfile" ]; then
  DOCKERFILE_SOURCE="$APP_DIR/Dockerfile"
else
  echo "ERROR: No Dockerfile found in project root or app folder."
  exit 1
fi

# -----------------------------
# Helpers
# -----------------------------
wait_for_service_ready() {
  local service_arn="$1"
  local region="$2"
  local max_attempts="${3:-60}"
  local sleep_seconds="${4:-10}"

  echo "==> Waiting for App Runner service to be ready for update..."

  for ((i=1; i<=max_attempts; i++)); do
    local status
    status="$(aws apprunner describe-service \
      --service-arn "$service_arn" \
      --region "$region" \
      --query 'Service.Status' \
      --output text)"

    echo "   attempt $i/$max_attempts -> status=$status"

    case "$status" in
      RUNNING|CREATE_FAILED|DELETE_FAILED|PAUSED)
        echo "==> Service is in a stable state: $status"
        return 0
        ;;
      OPERATION_IN_PROGRESS)
        sleep "$sleep_seconds"
        ;;
      *)
        echo "==> Service is in unexpected state: $status"
        sleep "$sleep_seconds"
        ;;
    esac
  done

  echo "ERROR: Timed out waiting for service to leave OPERATION_IN_PROGRESS"
  return 1
}

# -----------------------------
# Build temporary Docker context
# -----------------------------
BUILD_CTX="$(mktemp -d)"
cleanup() {
  rm -rf "$BUILD_CTX"
}
trap cleanup EXIT

echo "==> Preparing build context in $BUILD_CTX"

mkdir -p "$BUILD_CTX/app"
cp -R "$APP_DIR"/. "$BUILD_CTX/app/"

cp "$DOCKERFILE_SOURCE" "$BUILD_CTX/Dockerfile"

if [ -f "$APP_DIR/wsgi.py" ]; then
  cp "$APP_DIR/wsgi.py" "$BUILD_CTX/wsgi.py"
elif [ -f "./wsgi.py" ]; then
  cp "./wsgi.py" "$BUILD_CTX/wsgi.py"
else
  echo "ERROR: No wsgi.py found in app folder or project root."
  exit 1
fi

if [ -d "./instance" ]; then
  cp -R "./instance" "$BUILD_CTX/instance"
fi

echo "==> Build context contents:"
find "$BUILD_CTX" -maxdepth 3 | sort

# -----------------------------
# Ensure ECR repo exists
# -----------------------------
echo "==> Ensuring ECR repo exists"
aws ecr describe-repositories \
  --repository-names "$ECR_REPO" \
  --region "$AWS_DEFAULT_REGION" >/dev/null 2>&1 || \
aws ecr create-repository \
  --repository-name "$ECR_REPO" \
  --region "$AWS_DEFAULT_REGION" >/dev/null

# -----------------------------
# Login to ECR
# -----------------------------
echo "==> Logging into ECR"
aws ecr get-login-password --region "$AWS_DEFAULT_REGION" \
| docker login --username AWS --password-stdin \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com"

# -----------------------------
# Build, tag, push image
# -----------------------------
echo "==> Building Docker image"
docker build --no-cache -t "$ECR_REPO:latest" "$BUILD_CTX"

echo "==> Tagging image"
docker tag "$ECR_REPO:latest" "$IMAGE_URI"

echo "==> Pushing image"
docker push "$IMAGE_URI"

# -----------------------------
# Runtime environment
# -----------------------------
if [ -f "app-runner-env.json" ]; then
  ENV_JSON="$(cat app-runner-env.json)"
else
  ENV_JSON='{"RuntimeEnvironmentVariables":[
    {"name":"FLASK_ENV","value":"production"},
    {"name":"UPLOAD_ROOT","value":"/app/uploads"}
  ]}'
fi

# -----------------------------
# Create or update App Runner
# -----------------------------
echo "==> Checking for existing App Runner service"
SERVICE_ARN="$(aws apprunner list-services \
  --region "$AWS_DEFAULT_REGION" \
  --query "ServiceSummaryList[?ServiceName=='$APP_RUNNER_SERVICE'].ServiceArn" \
  --output text)"

SOURCE_CONFIG="$(jq -nc \
  --arg image "$IMAGE_URI" \
  --argjson env "$ENV_JSON" \
  '{
    ImageRepository: {
      ImageIdentifier: $image,
      ImageRepositoryType: "ECR",
      ImageConfiguration: {
        Port: "8080",
        RuntimeEnvironmentVariables: (
          ($env.RuntimeEnvironmentVariables // [])
          | map({(.name): .value})
          | add
        )
      }
    },
    AutoDeploymentsEnabled: true
  }')"

INSTANCE_CONFIG='{"Cpu":"1024","Memory":"2048"}'

if [ -z "$SERVICE_ARN" ] || [ "$SERVICE_ARN" = "None" ]; then
  echo "==> Creating App Runner service: $APP_RUNNER_SERVICE"
  CREATE_OUT="$(aws apprunner create-service \
    --service-name "$APP_RUNNER_SERVICE" \
    --source-configuration "$SOURCE_CONFIG" \
    --instance-configuration "$INSTANCE_CONFIG" \
    --region "$AWS_DEFAULT_REGION")"

  SERVICE_ARN="$(echo "$CREATE_OUT" | jq -r '.Service.ServiceArn')"
else
  wait_for_service_ready "$SERVICE_ARN" "$AWS_DEFAULT_REGION"

  echo "==> Updating App Runner service: $APP_RUNNER_SERVICE"
  for attempt in 1 2 3; do
    if aws apprunner update-service \
      --service-arn "$SERVICE_ARN" \
      --source-configuration "$SOURCE_CONFIG" \
      --region "$AWS_DEFAULT_REGION" >/dev/null; then
      echo "==> Update submitted successfully"
      break
    fi

    if [ "$attempt" -lt 3 ]; then
      echo "==> Update attempt $attempt failed, waiting and retrying..."
      sleep 15
      wait_for_service_ready "$SERVICE_ARN" "$AWS_DEFAULT_REGION"
    else
      echo "ERROR: App Runner update failed after 3 attempts"
      exit 1
    fi
  done
fi

# -----------------------------
# Show service URL/status
# -----------------------------
echo "==> Fetching service status"
aws apprunner describe-service \
  --service-arn "$SERVICE_ARN" \
  --region "$AWS_DEFAULT_REGION" \
  --query 'Service.{Status:Status,ServiceUrl:ServiceUrl}' \
  --output table

echo "==> Done"
echo "App Runner may take a few minutes to finish provisioning."