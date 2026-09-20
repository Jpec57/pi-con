param(
  [Parameter(Mandatory = $true)] [string] $HostName,
  [string] $User = "pi",
  [string] $Image = "pi-con:latest",
  [string] $Platform = "linux/arm64",
  [string] $TargetDir = "/opt/pi-con"
)

$ErrorActionPreference = "Stop"
docker buildx build --platform $Platform --load -t $Image .
docker save $Image | ssh "$User@$HostName" "mkdir -p $TargetDir/config && docker load"
scp config/settings.json "$User@$HostName`:$TargetDir/config/settings.json"
ssh "$User@$HostName" "cd $TargetDir && docker rm -f pi-con 2>/dev/null || true; docker run -d --name pi-con --restart unless-stopped --device /dev/snd --group-add audio -v $TargetDir/config:/app/config:ro $Image"
