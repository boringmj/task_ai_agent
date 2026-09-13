#!/bin/sh
# PHP 动态去混淆:把 eval 改写成捕获函数后再运行,截获运行时解出的代码。
#
# 用法:  sh php_hook.sh <样本.php> [输出文件]
# 安全:  样本会被真实执行,只能在 VM 沙箱内运行,禁止在宿主或容器上跑。
#
# 注意:vm_push 上传后换行会变成 CRLF,先 `sed -i 's/\r$//' php_hook.sh` 再跑。
set -e

sample="$1"
out="${2:-php_extracted.php}"
if [ -z "$sample" ]; then
  echo "用法: sh php_hook.sh <样本.php> [输出文件]"
  exit 1
fi

# busybox 的 mktemp 要求模板以 XXXXXX 结尾,不能带后缀
tmp="$(mktemp /tmp/deobf.XXXXXX)"
: > "$out"

# 注入捕获函数:先落盘再执行原 eval 逻辑
cat > "$tmp" <<'PHP'
<?php
function __deobf_dump($code) {
    $f = getenv('DEOBF_OUT') ?: 'php_extracted.php';
    file_put_contents($f, "<?php\n// ---- via eval ----\n" . $code . "\n", FILE_APPEND);
    return eval($code);
}
PHP

# 去掉样本开头的 <?php,并把 eval( / assert( 改写成捕获函数
sed -e '1s/^<?php//' \
    -e 's/eval[[:space:]]*(/__deobf_dump(/g' \
    -e 's/assert[[:space:]]*(/__deobf_dump(/g' \
    "$sample" >> "$tmp"

DEOBF_OUT="$out" php "$tmp"
echo "[OK] 运行完毕,捕获内容见 $out"
