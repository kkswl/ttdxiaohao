# GHCR 软件包

GHCR 镜像是一个便于分发和追溯的 OCI 软件包容器，容器内包含可直接导入 TgtoDrive 的插件 ZIP：

```text
/package/tgto-115-accounts-0.2.3-preview.zip
/package/tgto-115-accounts-0.2.3-source.zip
/package/package-info.json
/package/README.md
```

拉取并提取安装包：

```bash
docker pull ghcr.io/kkswl/ttdxiaohao:0.2.3
docker create --name tgto-package ghcr.io/kkswl/ttdxiaohao:0.2.3
docker cp tgto-package:/package/tgto-115-accounts-0.2.3-preview.zip .
docker rm tgto-package
```

如需检查包内文件，可以进入只读容器：

```bash
docker run --rm -it --entrypoint /bin/sh ghcr.io/kkswl/ttdxiaohao:0.2.3
```

该镜像用于分发插件文件，不是 TgtoDrive 的运行容器。
