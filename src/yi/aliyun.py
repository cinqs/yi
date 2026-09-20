"""Dependency-free Alibaba Cloud client for the two APIs yi needs.

Only the RPC-flavoured OpenAPI surface is used (ECS 2014-05-26, Alidns 2015-01-09),
which means the whole client is `hmac` + `urllib` from the standard library.

Two facts about Alibaba Cloud shaped this module:

* Spot (抢占式) instances cannot be created through `RunInstances`; only the older
  `CreateInstance` action accepts `SpotStrategy`.
* `CreateInstance` in a VPC needs an explicit `VSwitchId`, so we resolve the
  default VPC / vSwitch of the target zone instead of guessing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as _uuid
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("yi.aliyun")

ECS_VERSION = "2014-05-26"
ALIDNS_VERSION = "2015-01-09"
BSS_VERSION = "2017-12-14"

DEFAULT_ENDPOINTS = {
    "ecs": "https://ecs.aliyuncs.com/",
    "alidns": "https://alidns.aliyuncs.com/",
    # Billing/quota lives on its own gateway; it is how we check for arrears.
    "bss": "https://business.aliyuncs.com/",
}

RETRYABLE_CODES = {
    "Throttling",
    "Throttling.User",
    "Throttling.Api",
    "ServiceUnavailable",
    "InternalError",
    "RequestLimitExceeded",
}


# 所有阿里云 API 调用**一律不走系统代理**，三条理由：
#
#  1. 阿里云 API 本身在国内可达，走代理没有任何好处；
#  2. 本地代理一旦停掉（用户点了"断开"、内核崩了），请求会变成
#     `ECONNREFUSED` —— 而看门狗会把它误判成"实例被回收"，进而重建机器。
#     这个坑真实发生过：日志里一串 Connection refused 后面紧跟着
#     "第 N 次查不到实例（可能是竞价回收）"。
#  3. 这些请求带着 AccessKey 签名，没有理由让它经过第三方代理。
#
# urllib 默认会读取 macOS 的系统代理设置并**缓存**下来（`build_opener` 只建一次），
# 所以要显式用一个清空代理的 opener。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AliyunError(RuntimeError):
    """A structured error returned by the Alibaba Cloud gateway."""

    def __init__(
        self,
        code: str,
        message: str,
        request_id: str | None = None,
        http_status: int | None = None,
        action: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.request_id = request_id
        self.http_status = http_status
        self.action = action
        detail = f"{code}: {message}"
        if request_id:
            detail += f" (RequestId: {request_id})"
        super().__init__(detail)

    def hint(self) -> str | None:
        """A human hint for the errors users actually hit."""
        if self.code in ("InvalidParameter", "InvalidParam"):
            named = re.search(r'"([A-Za-z0-9_.]+)"', self.message or "")
            if named:
                return (
                    f"阿里云不接受参数 {named.group(1)} 的取值。这个参数由 yi 拼装，"
                    "先把 config.toml 里对应的项留空再试。"
                )
            return "提交的参数里有取值不合法，日志里带 RequestId，可对照阿里云 CreateInstance 文档排查。"
        hints = {
            "InvalidAccessKeyId.NotFound": "AccessKey 不存在，检查 ALIBABA_CLOUD_ACCESS_KEY_ID 或 ~/.aliyun/config.json 的 profile。",
            "SignatureDoesNotMatch": "AccessKeySecret 不对（注意别把 ID 和 Secret 写反）。",
            "Forbidden.RAM": "RAM 子账号缺少权限，参考 README 的《最小权限策略》一节。",
            "Forbidden.AccessKey": "该 AccessKey 被禁用或过期。",
            "InvalidInstanceType.ValueNotSupported": "该规格在目标可用区不可用，换一个 instance_types 候选。",
            "InvalidInstanceType.NotSupported": "该规格不支持竞价，换一个候选。",
            "Zone.NotOnSale": "该可用区停售，换一个 zone。",
            "InstanceType.StockNotEnough": "库存不足，换规格或可用区重试。",
            "InvalidSpotPriceLimit.LessThanMarketPrice": "出价低于市场价，提高 spot_price_limit 或改用 SpotAsPriceGo。",
            "InvalidSpotDuration": "保护期参数不合法：设置 spot_duration 时必须使用 SpotWithPriceLimit 并给出 spot_price_limit。",
            "InstanceStoppedEarly": (
                "抢占式实例在启动前被回收，或者账号余额/信用不足。"
                "默认配置（SpotWithPriceLimit + 1 小时保护期）能挡住前者；"
                "如果反复出现，去控制台看该实例的「事件」和账号余额。"
            ),
            "InvalidImageId.NotFound": "镜像 ID 在目标区域不存在，检查 image_name_filter。",
            "InvalidResourceType.NotSupported": "该资源类型在此区域不支持。",
            "InvalidSecurityGroupId.NotFound": "安全组不存在，可能已被手动删除，先执行 yi down --orphans。",
        }
        return hints.get(self.code)

    def is_retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


class Credentials:
    """AccessKey pair plus where it came from (never logged)."""

    def __init__(self, access_key_id: str, access_key_secret: str, source: str) -> None:
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.source = source

    def masked_id(self) -> str:
        if len(self.access_key_id) <= 8:
            return "****"
        return self.access_key_id[:4] + "****" + self.access_key_id[-4:]


def load_credentials(profile: str = "default", home: str | None = None) -> Credentials:
    """Resolve credentials: environment first, then the aliyun CLI config file."""
    env_id = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID") or os.environ.get("ALIYUN_ACCESS_KEY_ID")
    env_secret = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET") or os.environ.get(
        "ALIYUN_ACCESS_KEY_SECRET"
    )
    if env_id and env_secret:
        return Credentials(env_id, env_secret, "env")

    root = home or os.path.expanduser("~")
    path = os.path.join(root, ".aliyun", "config.json")
    if not os.path.exists(path):
        raise AliyunError(
            "CredentialsNotFound",
            "找不到 AccessKey：设置 ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET，"
            f"或先执行 `aliyun configure` 生成 {path}。",
        )
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        raise AliyunError("CredentialsUnreadable", f"无法解析 {path}: {exc}") from exc

    profiles = payload.get("profiles") or []
    current = payload.get("current")
    chosen = None
    for item in profiles:
        if item.get("name") == profile:
            chosen = item
            break
    if chosen is None and profile == "default" and current:
        for item in profiles:
            if item.get("name") == current:
                chosen = item
                break
    if chosen is None and len(profiles) == 1:
        chosen = profiles[0]
    if chosen is None:
        names = ", ".join(sorted(str(p.get("name")) for p in profiles)) or "(空)"
        raise AliyunError(
            "CredentialProfileNotFound",
            f"{path} 里没有 profile {profile!r}，现有：{names}",
        )
    key_id = chosen.get("access_key_id")
    key_secret = chosen.get("access_key_secret")
    if not key_id or not key_secret:
        raise AliyunError(
            "CredentialProfileIncomplete",
            f"profile {profile!r} 缺少 access_key_id / access_key_secret",
        )
    # Report the profile we actually picked, not the one that was asked for:
    # "default" often resolves through `current`, and the log should not lie about it.
    chosen_name = str(chosen.get("name") or profile)
    return Credentials(key_id, key_secret, f"{path}#{chosen_name}")


def percent_encode(value: Any) -> str:
    """RFC3986 encoding as required by the Alibaba Cloud signature algorithm."""
    return urllib.parse.quote(str(value), safe="~")


def canonical_query(params: dict[str, Any]) -> str:
    items = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        items.append(f"{percent_encode(key)}={percent_encode(value)}")
    return "&".join(items)


def sign(params: dict[str, Any], access_key_secret: str) -> str:
    string_to_sign = "POST&%2F&" + percent_encode(canonical_query(params))
    digest = hmac.new(
        (access_key_secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class RpcClient:
    """Shared signing/retry plumbing for RPC-style Alibaba Cloud APIs."""

    def __init__(
        self,
        credentials: Credentials,
        endpoint: str,
        version: str,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.credentials = credentials
        self.endpoint = endpoint
        self.version = version
        self.timeout = timeout
        self.max_retries = max_retries
        self.calls: list[str] = []

    def _base_params(self, action: str) -> dict[str, Any]:
        return {
            "Action": action,
            "Format": "JSON",
            "Version": self.version,
            "AccessKeyId": self.credentials.access_key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": str(_uuid.uuid4()),
            "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def call(self, action: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        merged: dict[str, Any] = self._base_params(action)
        if params:
            merged.update({k: v for k, v in params.items() if v is not None})
        if kwargs:
            merged.update({k: v for k, v in kwargs.items() if v is not None})
        merged["Signature"] = sign(merged, self.credentials.access_key_secret)
        self.calls.append(action)

        body = urllib.parse.urlencode(merged).encode("utf-8")
        attempt = 0
        while True:
            attempt += 1
            request = urllib.request.Request(
                self.endpoint,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "yi/0.1 (+stdlib)",
                },
                method="POST",
            )
            try:
                # 用显式清空代理的 opener，理由见 _NO_PROXY_OPENER 上面那段
                with _NO_PROXY_OPENER.open(request, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8", "replace")
                    status = response.getcode()
            except urllib.error.HTTPError as exc:
                payload = exc.read().decode("utf-8", "replace")
                status = exc.code
            except urllib.error.URLError as exc:
                if attempt <= self.max_retries:
                    self._sleep(attempt, f"网络错误: {exc.reason}")
                    continue
                raise AliyunError("NetworkError", f"请求 {action} 失败: {exc.reason}", action=action) from exc

            data = self._parse(payload)
            # RPC errors carry a `Code`; BSS-style APIs use Code "200" for success,
            # so only a non-success code counts as a failure.
            code = data.get("Code")
            failed = bool(code) and str(code) not in ("200", "0")
            if data.get("Success") is False:
                failed = True
            if status >= 400 or failed:
                error = AliyunError(
                    str(data.get("Code") or f"HTTP{status}"),
                    str(data.get("Message") or payload[:400]),
                    data.get("RequestId"),
                    status,
                    action,
                )
                if error.is_retryable() and attempt <= self.max_retries:
                    self._sleep(attempt, error.code)
                    continue
                raise error
            return data

    def _sleep(self, attempt: int, reason: str) -> None:
        delay = min(2**attempt, 20)
        log.warning("重试 %s（第 %s 次）: %s", self.calls[-1], attempt, reason)
        time.sleep(delay)

    @staticmethod
    def _parse(payload: str) -> dict[str, Any]:
        if not payload.strip():
            return {}
        try:
            data = json.loads(payload)
        except ValueError:
            return {"Code": "MalformedResponse", "Message": payload[:400]}
        return data if isinstance(data, dict) else {"Code": "MalformedResponse", "Message": str(data)[:400]}


def _instance_dict(payload: dict[str, Any]) -> dict[str, Any]:
    instances = _instance_list(payload)
    return instances[0] if instances else {}


def _instance_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    instances = payload.get("Instances", {}).get("Instance", [])
    if isinstance(instances, dict):
        instances = [instances]
    return [item for item in instances if isinstance(item, dict)]


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, dict):
        nested = value.get("IpAddress") or value.get("InstanceId") or []
        return _str_list(nested)
    return [str(value)]


class EcsClient(RpcClient):
    def __init__(
        self, credentials: Credentials, region: str, endpoint: str | None = None, **kwargs: Any
    ) -> None:
        # Region-specific endpoint: the global gateways handle some actions
        # inconsistently and answer others with
        # `InvalidOperation.NotSupportedEndpoint ... upgrade your SDK` (hit on DeleteInstance).
        endpoint = endpoint or f"https://ecs.{region}.aliyuncs.com/"
        super().__init__(credentials, endpoint, ECS_VERSION, **kwargs)
        self.region = region

    # -- discovery ---------------------------------------------------------
    def describe_regions(self) -> list[str]:
        data = self.call("DescribeRegions")
        regions = data.get("Regions", {}).get("Region", [])
        return [r.get("RegionId") for r in regions if r.get("RegionId")]

    def describe_zones(self) -> list[dict[str, Any]]:
        data = self.call("DescribeZones", RegionId=self.region)
        zones = data.get("Zones", {}).get("Zone", [])
        return zones if isinstance(zones, list) else [zones]

    def default_vswitch(self, zone_id: str) -> dict[str, str]:
        vpcs = self.call("DescribeVpcs", RegionId=self.region, IsDefault=True)
        items = vpcs.get("Vpcs", {}).get("Vpc", [])
        if isinstance(items, dict):
            items = [items]
        if not items:
            raise AliyunError(
                "DefaultVpcNotFound",
                f"区域 {self.region} 没有默认 VPC，请先在控制台创建默认 VPC，或手工指定 vpc_id/vswitch_id。",
            )
        vpc_id = items[0]["VpcId"]
        switches = (
            self.call("DescribeVSwitches", RegionId=self.region, VpcId=vpc_id, ZoneId=zone_id)
            .get("VSwitches", {})
            .get("VSwitch", [])
        )
        if isinstance(switches, dict):
            switches = [switches]
        if not switches:
            raise AliyunError(
                "DefaultVSwitchNotFound",
                f"默认 VPC {vpc_id} 在可用区 {zone_id} 没有交换机。",
            )
        return {"vpc_id": vpc_id, "vswitch_id": switches[0]["VSwitchId"]}

    def find_image(self, name_filters: Any, architecture: str = "x86_64") -> str:
        """Newest Alibaba Cloud public image whose name matches one of the filters.

        Alibaba Cloud names look like ``ubuntu_22_04_x64_20G_alibase_20240819.vhd``,
        so each filter is sent as a ``<filter>*`` prefix query and then re-checked
        locally before being accepted.
        """
        filters = [name_filters] if isinstance(name_filters, str) else list(name_filters or [])
        if not filters:
            raise AliyunError("ImageFilterMissing", "配置里没有 image_name_filters。")
        for name_filter in filters:
            seen: list[Any] = []
            for page in range(1, 4):
                data = self.call(
                    "DescribeImages",
                    RegionId=self.region,
                    ImageOwnerAlias="system",
                    OSType="linux",
                    ImageName=f"{name_filter}*",
                    PageNumber=page,
                    PageSize=100,
                )
                images = data.get("Images", {}).get("Image", [])
                if isinstance(images, dict):
                    images = [images]
                if not images:
                    break
                for image in images:
                    name = str(image.get("ImageName") or "")
                    arch = str(image.get("Architecture") or architecture)
                    if name_filter.lower() in name.lower() and arch == architecture:
                        seen.append((name, image.get("ImageId"), str(image.get("CreationTime") or "")))
            if seen:
                seen.sort(key=lambda item: item[2], reverse=True)
                log.info("选中镜像 %s (%s)", seen[0][0], seen[0][1])
                return str(seen[0][1])
            log.warning("镜像过滤 %r 没有命中，试下一个", name_filter)
        raise AliyunError(
            "ImageNotFound",
            f"在 {self.region} 找不到匹配 {filters} 的公共镜像。",
        )

    def key_pair_exists(self, name: str) -> bool:
        data = self.call("DescribeKeyPairs", RegionId=self.region, KeyPairName=name)
        pairs = data.get("KeyPairs", {}).get("KeyPair", [])
        if isinstance(pairs, dict):
            pairs = [pairs]
        return any(pair.get("KeyPairName") == name for pair in pairs)

    def import_key_pair(self, name: str, public_key_body: str) -> None:
        if self.key_pair_exists(name):
            log.info("密钥对 %s 已存在，跳过导入", name)
            return
        self.call(
            "ImportKeyPair",
            RegionId=self.region,
            KeyPairName=name,
            PublicKeyBody=public_key_body,
        )

    # -- security group ----------------------------------------------------
    def create_security_group(self, vpc_id: str, name: str, description: str) -> str:
        data = self.call(
            "CreateSecurityGroup",
            RegionId=self.region,
            VpcId=vpc_id,
            SecurityGroupName=name,
            Description=description,
        )
        return str(data["SecurityGroupId"])

    def authorize(
        self,
        group_id: str,
        ip_protocol: str,
        port_range: str,
        source_cidr: str,
        description: str,
        priority: int = 1,
    ) -> None:
        self.call(
            "AuthorizeSecurityGroup",
            RegionId=self.region,
            SecurityGroupId=group_id,
            IpProtocol=ip_protocol,
            PortRange=port_range,
            SourceCidrIp=source_cidr,
            Priority=priority,
            Description=description,
        )

    def authorize_egress_all(self, group_id: str) -> None:
        self.call(
            "AuthorizeSecurityGroupEgress",
            RegionId=self.region,
            SecurityGroupId=group_id,
            IpProtocol="all",
            PortRange="-1/-1",
            DestCidrIp="0.0.0.0/0",
            Priority=1,
            Description="yi allow all egress",
        )

    def delete_security_group(self, group_id: str) -> None:
        self.call("DeleteSecurityGroup", RegionId=self.region, SecurityGroupId=group_id)

    # -- reuse of pre-existing resources -----------------------------------
    def describe_vswitch(self, vswitch_id: str) -> dict[str, str]:
        """Resolve the zone/vpc a vSwitch lives in.

        A vSwitch belongs to exactly one zone, so pinning one also pins the zone
        and removes the multi-AZ fallback entirely.
        """
        data = self.call("DescribeVSwitches", RegionId=self.region, VSwitchId=vswitch_id)
        items = data.get("VSwitches", {}).get("VSwitch", [])
        if isinstance(items, dict):
            items = [items]
        if not items:
            raise AliyunError(
                "VSwitchNotFound",
                f"在 {self.region} 找不到交换机 {vswitch_id}。",
            )
        item = items[0]
        return {
            "vswitch_id": str(item["VSwitchId"]),
            "vpc_id": str(item["VpcId"]),
            "zone_id": str(item["ZoneId"]),
            "cidr_block": str(item.get("CidrBlock") or ""),
        }

    def security_group(self, group_id: str) -> dict[str, Any]:
        data = self.call("DescribeSecurityGroups", RegionId=self.region, SecurityGroupId=group_id)
        groups = data.get("SecurityGroups", {}).get("SecurityGroup", [])
        if isinstance(groups, dict):
            groups = [groups]
        if not groups:
            raise AliyunError(
                "SecurityGroupNotFound",
                f"在 {self.region} 找不到安全组 {group_id}。",
            )
        return groups[0]

    def security_group_rules(self, group_id: str, direction: str = "ingress") -> list[dict[str, Any]]:
        data = self.call(
            "DescribeSecurityGroupAttribute",
            RegionId=self.region,
            SecurityGroupId=group_id,
            Direction=direction,
        )
        permissions = data.get("Permissions", {}).get("Permission", [])
        if isinstance(permissions, dict):
            permissions = [permissions]
        return permissions

    def ensure_ingress(
        self,
        group_id: str,
        ip_protocol: str,
        port_range: str,
        source_cidr: str,
        description: str,
    ) -> bool:
        """Add an inbound rule only when an equivalent one is missing."""
        for rule in self.security_group_rules(group_id, "ingress"):
            same_protocol = str(rule.get("IpProtocol") or "").lower() == ip_protocol.lower()
            if (
                same_protocol
                and str(rule.get("PortRange") or "") == port_range
                and str(rule.get("SourceCidrIp") or "") == source_cidr
            ):
                log.info("安全组已有 %s %s <- %s，跳过", ip_protocol, port_range, source_cidr)
                return False
        try:
            self.authorize(group_id, ip_protocol, port_range, source_cidr, description)
        except AliyunError as exc:
            if exc.code != "InvalidPermission.Duplicate":
                raise
        log.info("安全组已放行 %s %s <- %s", ip_protocol, port_range, source_cidr)
        return True

    def ensure_egress_all(self, group_id: str) -> bool:
        for rule in self.security_group_rules(group_id, "egress"):
            if (
                str(rule.get("IpProtocol") or "").lower() == "all"
                and str(rule.get("DestCidrIp") or "") == "0.0.0.0/0"
            ):
                log.info("安全组已有全放行出方向规则，跳过")
                return False
        try:
            self.authorize_egress_all(group_id)
        except AliyunError as exc:
            if exc.code != "InvalidPermission.Duplicate":
                raise
        log.info("安全组已放行全部出方向")
        return True

    # -- instances ---------------------------------------------------------
    def create_spot_instance(
        self,
        *,
        image_id: str,
        instance_type: str,
        zone_id: str,
        vswitch_id: str,
        security_group_id: str,
        key_pair_name: str,
        user_data: str,
        instance_name: str,
        bandwidth_out: int,
        disk_category: str,
        disk_size: int,
        spot_strategy: str,
        spot_duration: int = 0,
        spot_price_limit: float = 0.0,
        bandwidth_in: int = 0,
    ) -> str:
        params: dict[str, Any] = {
            "RegionId": self.region,
            "ZoneId": zone_id,
            "ImageId": image_id,
            "InstanceType": instance_type,
            "VSwitchId": vswitch_id,
            "SecurityGroupId": security_group_id,
            "KeyPairName": key_pair_name,
            "InstanceName": instance_name,
            # HostName has its own rules (2-64 chars, alnum + hyphen, no leading/trailing
            # hyphen) which differ from InstanceName, so normalize instead of failing.
            "HostName": re.sub(r"[^A-Za-z0-9-]", "-", instance_name).strip("-")[:63] or "yi",
            "InstanceChargeType": "PostPaid",
            "SpotStrategy": spot_strategy,
            "Amount": 1,
            "InternetChargeType": "PayByTraffic",
            "InternetMaxBandwidthOut": bandwidth_out,
            "SystemDisk.Category": disk_category,
            "SystemDisk.Size": disk_size,
            "SystemDisk.DeleteWithInstance": "true",
            "UserData": base64.b64encode(user_data.encode("utf-8")).decode("ascii"),
        }
        # Inbound bandwidth is deliberately left at the API default:
        # `-1` (unlimited) is accepted by RunInstances but CreateInstance rejects it,
        # and under PayByTraffic inbound traffic is not billed anyway.
        if bandwidth_in:
            params["InternetMaxBandwidthIn"] = int(bandwidth_in)
        if spot_strategy == "SpotWithPriceLimit":
            if spot_price_limit <= 0:
                raise AliyunError(
                    "InvalidSpotPriceLimit",
                    "SpotWithPriceLimit 需要正的 spot_price_limit（单位：元/小时）。",
                )
            params["SpotPriceLimit"] = round(spot_price_limit, 3)
            if spot_duration:
                params["SpotDuration"] = spot_duration
        elif spot_strategy == "NoSpot":
            # 普通按量付费：不传 SpotStrategy 相关参数
            params.pop("SpotStrategy", None)
        elif spot_duration:
            log.info("SpotAsPriceGo 下忽略 spot_duration=%s（保护期需要 SpotWithPriceLimit）", spot_duration)
        data = self.call("CreateInstance", params)
        return str(data["InstanceId"])

    def describe_instance(self, instance_id: str) -> dict[str, Any]:
        data = self.call("DescribeInstances", RegionId=self.region, InstanceIds=json.dumps([instance_id]))
        instance = _instance_dict(data)
        if instance:
            return instance
        # Measured: some Alibaba POPs answer an ID-filtered query with "nothing" for an
        # instance that a plain listing happily returns (and DeleteInstance then fails
        # with InvalidInstanceId.NotFound). Listing and filtering locally is the fix.
        return self.find_in_listing(instance_id) or {}

    def find_in_listing(self, instance_id: str) -> dict[str, Any] | None:
        data = self.call("DescribeInstances", RegionId=self.region, PageSize=100)
        for item in _instance_list(data):
            if item.get("InstanceId") == instance_id:
                return item
        return None

    def instance_exists(self, instance_id: str) -> bool:
        return self.find_in_listing(instance_id) is not None

    def instance_status(self, instance_id: str) -> str:
        data = self.call(
            "DescribeInstanceStatus", RegionId=self.region, InstanceIds=json.dumps([instance_id])
        )
        statuses = data.get("InstanceStatuses", {}).get("InstanceStatus", [])
        if isinstance(statuses, dict):
            statuses = [statuses]
        if not statuses:
            return "Gone"
        return str(statuses[0].get("Status") or "Unknown")

    def instance_details(self, instance_id: str) -> dict[str, Any]:
        """Everything that explains *why* an instance is not running."""
        instance = self.describe_instance(instance_id)
        if not instance:
            return {"status": "Gone"}
        locks = instance.get("OperationLocks", {}).get("LockReason", [])
        if isinstance(locks, dict):
            locks = [locks]
        return {
            "status": instance.get("Status"),
            "stopped_mode": instance.get("StoppedMode"),
            "lock_reasons": [str(item.get("LockReason")) for item in locks if item],
            "instance_charge_type": instance.get("InstanceChargeType"),
            "spot_strategy": instance.get("SpotStrategy"),
            "internet_charge_type": instance.get("InternetChargeType"),
            "creation_time": instance.get("CreationTime"),
        }

    def spot_price(self, instance_type: str, zone_id: str) -> float | None:
        """Most recent peak spot price for this type/zone, in CNY/hour.

        Used to build a bid that buys the protection period without overpaying.
        Returns None when the API refuses, so callers can degrade loudly.
        """
        try:
            data = self.call(
                "DescribeSpotPriceHistory",
                RegionId=self.region,
                ZoneId=zone_id,
                InstanceType=instance_type,
                NetworkType="vpc",
                IoOptimized="optimized",
            )
        except AliyunError as exc:
            log.warning("查询 %s 在 %s 的竞价市场价失败: %s", instance_type, zone_id, exc)
            return None
        points = data.get("SpotPrices", {}).get("SpotPriceType", [])
        if isinstance(points, dict):
            points = [points]
        prices: list[float] = []
        for point in points:
            try:
                prices.append(float(point.get("SpotPrice")))
            except (TypeError, ValueError):
                continue
        if not prices:
            log.warning("竞价市场价返回空（%s @ %s）", instance_type, zone_id)
            return None
        peak = max(prices)
        log.info("竞价市场价参考（%s @ %s）: 近期峰值 %.3f 元/小时", instance_type, zone_id, peak)
        return peak

    def public_ip(self, instance_id: str) -> str | None:
        instance = self.describe_instance(instance_id)
        ips = _str_list(instance.get("PublicIpAddress"))
        if ips:
            return ips[0]
        eip = instance.get("EipAddress", {})
        return eip.get("IpAddress") or None

    def delete_instance(self, instance_id: str, force: bool = True) -> None:
        self.call("DeleteInstance", RegionId=self.region, InstanceId=instance_id, Force=force)

    def delete_instance_when_ready(
        self, instance_id: str, timeout: float = 120.0, interval: float = 5.0
    ) -> bool:
        """Delete an instance, retrying while it is still Initializing.

        A freshly created instance cannot be deleted for the first few seconds
        (`IncorrectInstanceStatus.Initializing`). A rollback that gives up here
        leaves a *billing* instance behind, which is exactly what happened once.
        """
        # Two kinds of transient failure are expected here:
        #   * IncorrectInstanceStatus.Initializing — too early to delete
        #   * InvalidInstanceId.NotFound / NotSupportedEndpoint — request landed on a
        #     POP that cannot see the instance (measured); retrying usually lands on one
        #     that can.
        retryable = {
            "IncorrectInstanceStatus.Initializing",
            "IncorrectInstanceStatus.Starting",
            "IncorrectInstanceStatus",
        }
        deadline = time.time() + timeout
        last_error: AliyunError | None = None
        missing_streak = 0
        while time.time() < deadline:
            try:
                self.delete_instance(instance_id, force=True)
                log.info("已删除实例 %s", instance_id)
                return True
            except AliyunError as exc:
                last_error = exc
                if (
                    exc.code == "InvalidOperation.NotSupportedEndpoint"
                    or exc.code == "InvalidInstanceId.NotFound"
                ):
                    # The answer is unreliable: a POP that cannot see the instance says
                    # "not found" while another still lists it. Only conclude it is gone
                    # after several consecutive misses, otherwise we would report a
                    # successful cleanup while the instance keeps billing.
                    missing_streak = 0 if self.instance_exists(instance_id) else missing_streak + 1
                    log.info("删除返回 %s，实例存在性确认第 %d 次失败", exc.code, missing_streak)
                    if missing_streak >= 3:
                        log.info("连续 3 次都查不到实例 %s，判定已释放", instance_id)
                        return True
                elif exc.code not in retryable:
                    raise
                time.sleep(interval)
        log.error("实例 %s 在 %ds 内始终无法删除: %s", instance_id, int(timeout), last_error)
        return False

    def wait_running(self, instance_id: str, timeout: float = 240.0, interval: float = 5.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        last = "Unknown"
        while time.time() < deadline:
            status = self.instance_status(instance_id)
            if status != last:
                log.info("实例状态: %s", status)
                last = status
            if status == "Running":
                return self.describe_instance(instance_id)
            if status in {"Gone", "Deleted"}:
                raise AliyunError("InstanceVanished", f"实例 {instance_id} 创建后消失（可能是竞价被回收）。")
            if status in {"Stopped", "Stopping"}:
                # A brand-new instance that stops instead of starting is not a slow
                # boot: it is a reclaimed spot instance (or an account in arrears).
                # Waiting the full timeout only makes the user stare at a hung prompt.
                details = self.instance_details(instance_id)
                raise AliyunError(
                    "InstanceStoppedEarly",
                    "实例 {} 创建后直接进入 {}（停止模式 {}，锁定原因 {}）。".format(
                        instance_id,
                        details.get("status"),
                        details.get("stopped_mode") or "-",
                        ",".join(details.get("lock_reasons") or []) or "无",
                    ),
                )
            time.sleep(interval)
        raise AliyunError(
            "InstanceStartTimeout",
            f"等待实例 {instance_id} 进入 Running 超时（最后状态 {last}）。",
        )

    def ensure_running(self, instance_id: str, timeout: float = 300.0, interval: float = 5.0) -> None:
        """Wait for Running, but if Aliyun left the instance Stopped, start it.

        Measured twice on this account: a freshly created instance (spot *and*
        pay-as-you-go) sat in `Stopped`/`KeepCharging` with no public IP, and never
        recovered on its own — but an explicit StartInstance brought it up in ~10s.
        Refusing to start it would have thrown away a perfectly good machine.
        """
        deadline = time.time() + timeout
        start_issued = False
        last = "Unknown"
        while time.time() < deadline:
            status = self.instance_status(instance_id)
            if status != last:
                log.info("实例状态: %s", status)
                last = status
            if status == "Running":
                return
            if status in {"Gone", "Deleted"}:
                raise AliyunError("InstanceVanished", f"实例 {instance_id} 创建后消失。")
            if status in {"Stopped", "Stopping"} and not start_issued:
                details = self.instance_details(instance_id)
                log.warning(
                    "实例创建后是 %s（停止模式 %s，锁定 %s），显式启动它",
                    status,
                    details.get("stopped_mode") or "-",
                    ",".join(details.get("lock_reasons") or []) or "无",
                )
                self.call("StartInstance", RegionId=self.region, InstanceId=instance_id)
                start_issued = True
            time.sleep(interval)
        raise AliyunError(
            "InstanceStartTimeout",
            f"实例 {instance_id} 在 {int(timeout)}s 内没有进入 Running（最后状态 {last}）。",
        )

    def ensure_public_ip(self, instance_id: str, timeout: float = 90.0, interval: float = 6.0) -> str:
        """Return the public IP, allocating one if the instance was created without it.

        The legacy `CreateInstance` API does not reliably attach a public IPv4 to a
        VPC instance even when `InternetMaxBandwidthOut` is set (measured: the field
        came back empty), so this falls back to `AllocatePublicIpAddress`.
        """
        last_error: AliyunError | None = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.public_ip(instance_id)
            if found:
                return found
            try:
                log.info("实例没有公网 IP，调用 AllocatePublicIpAddress")
                response = self.call("AllocatePublicIpAddress", RegionId=self.region, InstanceId=instance_id)
                if response.get("IpAddress"):
                    return str(response["IpAddress"])
            except AliyunError as exc:
                last_error = exc
                log.warning("分配公网 IP 失败: %s", exc)
            time.sleep(interval)
        raise AliyunError(
            "NoPublicIp",
            "实例 {} 没有公网 IP，自动分配也失败了{}。".format(
                instance_id, f"（{last_error}）" if last_error else ""
            ),
        )

    def internet_tx_bytes(self, instance_id: str, start: datetime, end: datetime) -> int:
        data = self.call(
            "DescribeInstanceMonitorData",
            RegionId=self.region,
            InstanceId=instance_id,
            StartTime=start.strftime("%Y-%m-%dT%H:%MZ"),
            EndTime=end.strftime("%Y-%m-%dT%H:%MZ"),
        )
        points = data.get("MonitorData", {}).get("InstanceMonitorData", [])
        if isinstance(points, dict):
            points = [points]
        total = 0
        for point in points:
            try:
                total += int(point.get("InternetTX") or 0)
            except (TypeError, ValueError):
                continue
        return total


class AlidnsClient(RpcClient):
    """Optional: keeps a stable A record pointing at the current spot instance."""

    def __init__(self, credentials: Credentials, **kwargs: Any) -> None:
        super().__init__(credentials, DEFAULT_ENDPOINTS["alidns"], ALIDNS_VERSION, **kwargs)

    def upsert_a_record(self, domain: str, sub_domain: str, ip: str, ttl: int = 60) -> str:
        existing = (
            self.call(
                "DescribeDomainRecords",
                DomainName=domain,
                RRKeyWord=sub_domain,
                TypeKeyWord="A",
            )
            .get("DomainRecords", {})
            .get("Record", [])
        )
        if isinstance(existing, dict):
            existing = [existing]
        for record in existing:
            if record.get("RR") == sub_domain and record.get("Type") == "A":
                if record.get("Value") == ip:
                    return str(record.get("RecordId"))
                self.call(
                    "UpdateDomainRecord",
                    RecordId=record["RecordId"],
                    RR=sub_domain,
                    Type="A",
                    Value=ip,
                    TTL=ttl,
                )
                return str(record["RecordId"])
        data = self.call(
            "AddDomainRecord",
            DomainName=domain,
            RR=sub_domain,
            Type="A",
            Value=ip,
            TTL=ttl,
        )
        return str(data.get("RecordId"))


class BssClient(RpcClient):
    """Billing queries: tells 'capacity reclaimed' apart from 'account out of money'."""

    def __init__(self, credentials: Credentials, **kwargs: Any) -> None:
        super().__init__(credentials, DEFAULT_ENDPOINTS["bss"], BSS_VERSION, **kwargs)

    def account_balance(self) -> dict[str, Any]:
        data = self.call("QueryAccountBalance")
        payload = data.get("Data")
        return payload if isinstance(payload, dict) else {}
