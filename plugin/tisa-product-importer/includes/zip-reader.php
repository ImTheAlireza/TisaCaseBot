<?php
/** Bounded ZIP extraction into private, expiring, non-web staging directories. */
if (!defined('ABSPATH')) exit;

final class Tisa_Zip_Reader {
    const MAX_EXPANDED = 134217728;
    const MAX_ARCHIVE = 67108864;

    public static function init() {
        add_action('tisa_zip_sweep', [__CLASS__, 'sweep']);
        if (!wp_next_scheduled('tisa_zip_sweep')) wp_schedule_event(time() + 3600, 'hourly', 'tisa_zip_sweep');
    }

    private static function root() {
        $root = rtrim(sys_get_temp_dir(), DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR . 'tisa-private-' . substr(hash('sha256', ABSPATH), 0, 16);
        if (!is_dir($root) && !mkdir($root, 0700, true)) throw new RuntimeException('پوشهٔ خصوصی ZIP قابل ساخت نیست');
        chmod($root, 0700);
        $site = realpath(ABSPATH); $actual = realpath($root);
        if (!$actual || $site && strpos($actual . '/', $site . '/') === 0) throw new RuntimeException('پوشهٔ موقت ZIP نباید داخل ریشهٔ عمومی سایت باشد');
        return $root;
    }

    public static function remove($dir) {
        if (!$dir || !is_dir($dir)) return;
        $root = realpath(self::root()); $actual = realpath($dir);
        if (!$actual || strpos($actual . '/', $root . '/') !== 0 || $actual === $root) throw new RuntimeException('حذف مسیر خارج از staging ممنوع است');
        $items = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($actual, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST);
        foreach ($items as $item) {
            if ($item->isDir() && !$item->isLink()) rmdir($item->getPathname()); else unlink($item->getPathname());
        }
        rmdir($actual);
    }

    public static function sweep() {
        foreach (glob(self::root() . '/batch-*', GLOB_ONLYDIR) ?: [] as $dir) {
            $handle = @fopen($dir . '/.active', 'c');
            if (!$handle) continue;
            try {
                if (!flock($handle, LOCK_EX | LOCK_NB)) continue;
                if (filemtime($dir) < time() - 2 * HOUR_IN_SECONDS) self::remove($dir);
            } finally { fclose($handle); }
        }
    }

    public static function read($path) {
        if (!class_exists('ZipArchive')) throw new RuntimeException('PHP ZipArchive روی هاست فعال نیست');
        if (!is_file($path) || filesize($path) > self::MAX_ARCHIVE) throw new RuntimeException('فایل ZIP از سقف ۶۴ مگابایت بیشتر است');
        $dir = self::root() . '/batch-' . bin2hex(random_bytes(16));
        if (!mkdir($dir, 0700)) throw new RuntimeException('ساخت staging ناموفق بود');
        $archive = new ZipArchive();
        try {
            if ($archive->open($path) !== true || $archive->numFiles > 80) throw new RuntimeException('ZIP نامعتبر یا دارای فایل‌های بسیار زیاد است');
            $total = 0; $seen = []; $manifest = null; $images = [];
            for ($index = 0; $index < $archive->numFiles; $index++) {
                $entry = $archive->statIndex($index); $name = $entry['name'];
                if (strpos($name, "\0") !== false || preg_match('~(^[/\\\\]|[A-Za-z]:|(^|[/\\\\])\.\.([/\\\\]|$))~', $name)) throw new RuntimeException('ZIP شامل مسیر مطلق/غیرمجاز است');
                $archive->getExternalAttributesIndex($index, $opsys, $attrs);
                if (($attrs >> 16 & 0170000) === 0120000) throw new RuntimeException('symlink در ZIP مجاز نیست');
                if (substr($name, -1) === '/') continue;
                if (isset($seen[strtolower($name)])) throw new RuntimeException('نام فایل تکراری در ZIP');
                $seen[strtolower($name)] = true; $total += $entry['size'];
                if ($entry['size'] > 33554432 || $total > self::MAX_EXPANDED) throw new RuntimeException('حجم بازشدهٔ ZIP از سقف ایمن بیشتر است');
                $base = basename(str_replace('\\', '/', $name));
                $extension = strtolower(pathinfo($base, PATHINFO_EXTENSION));
                $json = strtolower($base) === 'product.json';
                if (!$json && !in_array($extension, ['jpg','jpeg','png','webp','gif'], true)) {
                    if (in_array(strtolower($base), ['readme.txt','caption.txt'], true)) continue;
                    throw new RuntimeException('نوع فایل داخل ZIP مجاز نیست');
                }
                if ($json && ($manifest !== null || $entry['size'] > 2097152)) throw new RuntimeException('product.json تکراری/بسیار بزرگ است');
                $target = $dir . '/' . sprintf('%03d_', $index) . $base;
                $input = $archive->getStream($name); $output = fopen($target, 'xb');
                if (!$input || !$output) throw new RuntimeException('باز کردن فایل ZIP ناموفق بود');
                chmod($target, 0600);
                try {
                    $copied = stream_copy_to_stream($input, $output, min($entry['size'] + 1, 33554433));
                    if ($copied !== $entry['size']) throw new RuntimeException('اندازهٔ واقعی فایل با header ZIP نمی‌خواند');
                } finally { fclose($input); fclose($output); }
                if ($json) { $manifest = json_decode(file_get_contents($target), true, 64, JSON_THROW_ON_ERROR); }
                else {
                    $dimensions = @getimagesize($target);
                    if (!$dimensions || $dimensions[0] * $dimensions[1] > 40000000) throw new RuntimeException('تصویر نامعتبر یا بیش از ۴۰ مگاپیکسل است');
                    $images[] = ['path'=>$target, 'name'=>$base];
                    if (count($images) > 30) throw new RuntimeException('بیش از ۳۰ تصویر مجاز نیست');
                }
            }
            if (!is_array($manifest)) throw new RuntimeException('product.json داخل ZIP پیدا نشد/معتبر نیست');
            return ['dir'=>$dir, 'data'=>Tisa_Product_Operations::normalize($manifest), 'images'=>$images];
        } catch (Throwable $error) { self::remove($dir); throw $error; }
        finally { $archive->close(); }
    }

    public static function upload($images) {
        require_once ABSPATH . 'wp-admin/includes/file.php';
        require_once ABSPATH . 'wp-admin/includes/media.php';
        require_once ABSPATH . 'wp-admin/includes/image.php';
        $out = [];
        try {
            foreach ($images as $image) {
                $dimensions = getimagesize($image['path']);
                $extensions = ['image/jpeg'=>'jpg','image/png'=>'png','image/webp'=>'webp','image/gif'=>'gif'];
                $extension = $extensions[$dimensions['mime'] ?? ''] ?? null;
                if (!$extension) throw new RuntimeException('نوع واقعی تصویر مجاز نیست');
                $name = pathinfo($image['name'], PATHINFO_FILENAME) . '.' . $extension;
                $id = media_handle_sideload(['name'=>$name, 'tmp_name'=>$image['path'], 'error'=>0, 'size'=>filesize($image['path'])], 0);
                if (is_wp_error($id) || !$id) throw new RuntimeException(is_wp_error($id) ? $id->get_error_message() : 'ذخیرهٔ تصویر تأیید نشد');
                $out[] = ['id'=>(int)$id, 'name'=>$image['name']];
            }
            return $out;
        } catch (Throwable $error) { self::rollback_uploads($out); throw $error; }
    }

    public static function rollback_uploads($images) {
        foreach ($images as $image) wp_delete_attachment($image['id'], true);
    }
}
