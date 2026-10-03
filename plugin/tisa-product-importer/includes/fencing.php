<?php
/** Native WooCommerce REST fencing and optimistic stock/parent guards. */
if (!defined('ABSPATH')) exit;

final class Tisa_REST_Fencing {
    const CONTRACT = 1;
    private static $locks = [];
    private static $transaction = false;

    public static function init() {
        add_action('rest_api_init', [__CLASS__, 'routes']);
        add_filter('woocommerce_rest_product_collection_params', [__CLASS__, 'params']);
        add_filter('woocommerce_rest_product_object_query', [__CLASS__, 'query'], 10, 2);
        add_filter('woocommerce_rest_pre_insert_product_object', [__CLASS__, 'parent'], PHP_INT_MAX, 3);
        add_filter('woocommerce_rest_pre_insert_product_variation_object', [__CLASS__, 'variation'], PHP_INT_MAX, 3);
        add_action('woocommerce_rest_insert_product_object', [__CLASS__, 'committed'], PHP_INT_MAX, 3);
        add_action('woocommerce_rest_insert_product_variation_object', [__CLASS__, 'committed'], PHP_INT_MAX, 3);
        register_shutdown_function([__CLASS__, 'release']);
    }

    public static function routes() {
        register_rest_route('wc/v3', '/tisa-health', [
            'methods' => 'GET', 'callback' => [__CLASS__, 'health'],
            'permission_callback' => function() { return current_user_can('edit_products'); },
        ]);
    }

    public static function transactional() {
        global $wpdb;
        $engine = $wpdb->get_var($wpdb->prepare(
            'SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s',
            $wpdb->postmeta
        ));
        return strtoupper((string)$engine) === 'INNODB';
    }

    public static function health() {
        return rest_ensure_response([
            'contract' => self::CONTRACT, 'batch_fencing' => true, 'variation_fencing' => true,
            'parent_cas' => true, 'stock_cas' => self::transactional(), 'max_variations' => 1000,
            'isolated_test' => defined('TISA_CONTRACT_ENV') && TISA_CONTRACT_ENV,
        ]);
    }

    public static function lock($key) {
        global $wpdb;
        $name = 'tisa_' . substr(hash('sha256', $key), 0, 48);
        if (isset(self::$locks[$name])) return true;
        $got = $wpdb->get_var($wpdb->prepare('SELECT GET_LOCK(%s, 10)', $name));
        if ((int)$got !== 1) return false;
        self::$locks[$name] = true;
        return true;
    }

    public static function release() {
        global $wpdb;
        if (self::$transaction) {
            $wpdb->query('ROLLBACK');
            self::$transaction = false;
        }
        foreach (array_keys(self::$locks) as $name) {
            $wpdb->get_var($wpdb->prepare('SELECT RELEASE_LOCK(%s)', $name));
        }
        self::$locks = [];
    }

    public static function batch($request) {
        foreach ((array)$request->get_param('meta_data') as $row) {
            if (is_array($row) && ($row['key'] ?? '') === 'tisa_batch_id') {
                $value = $row['value'] ?? '';
                return is_string($value) && preg_match('/^[a-zA-Z0-9_-]{1,64}$/D', $value) ? $value : '';
            }
        }
        return '';
    }

    public static function find_batch($batch) {
        global $wpdb;
        // Trash is deliberately included: an owner's deletion is not permission
        // for an old retry to recreate the same intent automatically.
        return (int)$wpdb->get_var($wpdb->prepare(
            "SELECT p.ID FROM {$wpdb->posts} p JOIN {$wpdb->postmeta} m ON p.ID=m.post_id " .
            "WHERE p.post_type='product' AND m.meta_key='tisa_batch_id' AND BINARY m.meta_value=%s ORDER BY p.ID LIMIT 1",
            $batch
        ));
    }

    public static function params($params) {
        $params['tisa_batch_id'] = ['type'=>'string', 'maxLength'=>64, 'pattern'=>'^[a-zA-Z0-9_-]+$'];
        return $params;
    }

    public static function query($args, $request) {
        $batch = $request->get_param('tisa_batch_id');
        if (is_string($batch) && preg_match('/^[a-zA-Z0-9_-]{1,64}$/D', $batch)) {
            if (!self::lock('batch:' . $batch)) throw new RuntimeException('Tisa batch read lock unavailable');
            unset($args['s']); // Renamed products must still be found by their immutable intent.
            $args['meta_query'][] = ['key'=>'tisa_batch_id', 'value'=>$batch, 'compare'=>'='];
        }
        return $args;
    }

    public static function text($value) {
        $s = html_entity_decode((string)$value, ENT_QUOTES, 'UTF-8');
        $s = strtr($s, array_combine(preg_split('//u', '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', -1, PREG_SPLIT_NO_EMPTY), str_split('01234567890123456789')));
        $s = str_replace(['ي','ك',"\u{200C}","\u{200F}"], ['ی','ک','',''], $s);
        return mb_strtolower(trim($s));
    }

    public static function key($name) {
        $key = preg_replace('/[^\p{L}\p{N}]+/u', '', self::text(urldecode(preg_replace('/^pa_/i', '', (string)$name))));
        if (in_array($key, ['مدل','مدلگوشی','model','models'], true)) return 'model';
        if (in_array($key, ['ایرپاد','ایرپادز','ایرپادس','airpod','airpods','appleairpods'], true)) return 'airpods';
        if (in_array($key, ['رنگ','رنگبندی','color','colors','colour','colours'], true)) return 'color';
        return $key;
    }

    public static function option($name, $value) {
        $s = self::text($value);
        if (in_array(self::key($name), ['model','airpods'], true)) {
            $s = preg_replace('/(iphone|apple|samsung|galaxy|xiaomi|redmi|poco|آیفون|ایفون|اپل|سامسونگ|گلکسی|شیائومی|ردمی|پوکو)/u', ' ', $s);
            $s = str_replace(['پرو','مکس','ماکس','مینی','پلاس','+'], ['pro','max','max','mini','plus','plus'], $s);
            $s = preg_replace('/air\s*pods?/i', 'airpods', $s);
        }
        // NEVER remove 4G/5G: these are different physical compatibility options.
        return preg_replace('/[^\p{L}\p{N}\/]+/u', '', $s);
    }

    public static function combo($attrs, $parent = null) {
        $out = [];
        foreach ((array)$attrs as $name => $value) {
            if ($parent && strpos($name, 'pa_') === 0) $name = wc_attribute_label($name, $parent);
            $out[self::key($name)] = self::option($name, $value);
        }
        ksort($out);
        return $out;
    }

    public static function parent($product, $request, $creating) {
        if (is_wp_error($product)) return $product;
        $batch = self::batch($request);
        if ($creating && $batch) {
            if (!self::lock('batch:' . $batch)) return new WP_Error('tisa_lock_failed', 'قفل انتشار در دسترس نیست', ['status'=>503]);
            $existing = self::find_batch($batch);
            if ($existing) return new WP_Error('tisa_batch_exists', 'این بسته قبلاً ساخته شده است', ['status'=>409, 'existing_product_id'=>$existing]);
        }
        $sku = (string)$product->get_sku('edit');
        if ($sku && ($creating || $request->has_param('sku'))) {
            $prefix = preg_match('/^([A-Z]+)[0-9]+$/D', $sku, $m) ? $m[1] : $sku;
            if (!self::lock('sku:all')) return new WP_Error('tisa_lock_failed', 'قفل SKU در دسترس نیست', ['status'=>503]);
        }
        if (!$creating && ($request->get_param('tisa_fence') || $request->get_param('tisa_expected_stock'))) {
            if (!self::lock('product:' . $product->get_id())) return new WP_Error('tisa_lock_failed', 'قفل محصول در دسترس نیست', ['status'=>503]);
            $guard = self::fields_guard($product->get_id(), $request);
            if (is_wp_error($guard)) return $guard;
            $guard = self::stock_guard($product->get_id(), $request);
            if (is_wp_error($guard)) return $guard;
        }
        return $product;
    }

    public static function variation($product, $request, $creating) {
        global $wpdb;
        if (is_wp_error($product)) return $product;
        $parent_id = (int)$product->get_parent_id();
        $managed = $request->get_param('tisa_fence') || get_post_meta($parent_id, 'tisa_batch_id', true);
        if (!$managed) return $product;
        if (!self::lock('product:' . $parent_id)) return new WP_Error('tisa_lock_failed', 'قفل واریژن در دسترس نیست', ['status'=>503]);
        if ($creating) {
            $parent = wc_get_product($parent_id);
            $wanted = self::combo($product->get_attributes(), $parent);
            if (!$wanted || in_array('', $wanted, true)) return new WP_Error('tisa_invalid_combination', 'واریژن با گزینهٔ خالی مجاز نیست', ['status'=>422]);
            $ids = $wpdb->get_col($wpdb->prepare("SELECT ID FROM {$wpdb->posts} WHERE post_parent=%d AND post_type='product_variation' AND post_status<>'trash' ORDER BY ID LIMIT 5001", $parent_id));
            if (count($ids) > 5000) return new WP_Error('tisa_grid_limit', 'محصول از سقف ایمن بررسی واریژن بیشتر است', ['status'=>413]);
            foreach ($ids as $id) {
                clean_post_cache($id);
                $existing = wc_get_product($id);
                if ($existing && self::combo($existing->get_attributes(), $parent) === $wanted) {
                    return new WP_Error('tisa_variation_exists', 'این ترکیب قبلاً ساخته شده است', ['status'=>409, 'existing_variation_id'=>(int)$id]);
                }
            }
        } else {
            $guard = self::stock_guard($product->get_id(), $request);
            if (is_wp_error($guard)) return $guard;
        }
        return $product;
    }

    public static function canonical_attrs($attrs) {
        $out = [];
        foreach ((array)$attrs as $attr) {
            $name = $attr['name'] ?? '';
            $values = array_map(function($value) use ($name) { return self::option($name, $value); }, (array)($attr['options'] ?? []));
            sort($values);
            $out[self::key($name)] = [(bool)($attr['variation'] ?? false), (bool)($attr['visible'] ?? false), $values];
        }
        ksort($out);
        return $out;
    }

    public static function fields_guard($id, $request) {
        $expected = $request->get_param('tisa_expected_fields');
        if (!is_array($expected)) return true;
        clean_post_cache($id);
        $current = wc_get_product($id);
        foreach ($expected as $key => $value) {
            if ($key === 'attributes') {
                $attrs = [];
                foreach ($current->get_attributes() as $attr) {
                    $options = $attr->is_taxonomy() ? wc_get_product_terms($id, $attr->get_name(), ['fields'=>'names']) : $attr->get_options();
                    $attrs[] = ['name'=>wc_attribute_label($attr->get_name(), $current), 'options'=>$options, 'visible'=>$attr->get_visible(), 'variation'=>$attr->get_variation()];
                }
                $same = self::canonical_attrs($attrs) === self::canonical_attrs($value);
            } elseif ($key === 'images') {
                $ids = array_filter(array_merge([$current->get_image_id()], $current->get_gallery_image_ids()));
                $same = array_values($ids) === array_column((array)$value, 'id');
            } elseif ($key === 'name') {
                $same = self::text($current->get_name('edit')) === self::text($value);
            } else {
                $getter = 'get_' . $key;
                $same = is_callable([$current, $getter]) && (in_array($key, ['regular_price','sale_price','stock_quantity'], true) ? (float)$current->$getter('edit') === (float)$value : (string)$current->$getter('edit') === (string)$value);
            }
            if (!$same) return new WP_Error('tisa_parent_changed', 'محصول پس از پیش‌نمایش تغییر کرده؛ دوباره بررسی و تأیید کنید', ['status'=>409]);
        }
        return true;
    }

    public static function stock_guard($id, $request) {
        global $wpdb;
        $expected = $request->get_param('tisa_expected_stock');
        if (!is_array($expected)) return true;
        if (!self::transactional()) return new WP_Error('tisa_storage_unsupported', 'کنترل موجودی به InnoDB نیاز دارد', ['status'=>503]);
        if (!self::$transaction) {
            if ($wpdb->query('START TRANSACTION') === false) return new WP_Error('tisa_lock_failed', 'شروع تراکنش موجودی ناموفق بود', ['status'=>503]);
            self::$transaction = true;
        }
        $rows = $wpdb->get_results($wpdb->prepare("SELECT meta_key, meta_value FROM {$wpdb->postmeta} WHERE post_id=%d AND meta_key IN ('_stock','_manage_stock') FOR UPDATE", $id), ARRAY_A);
        $meta = array_column($rows, 'meta_value', 'meta_key');
        $quantity = isset($meta['_stock']) && $meta['_stock'] !== '' ? (float)$meta['_stock'] : null;
        $same = ($expected['quantity'] ?? null) === null ? $quantity === null : $quantity === (float)$expected['quantity'];
        if (array_key_exists('managed', $expected)) $same = $same && (bool)$expected['managed'] === (($meta['_manage_stock'] ?? 'no') === 'yes');
        if (!$same) {
            $wpdb->query('ROLLBACK'); self::$transaction = false;
            return new WP_Error('tisa_stock_changed', 'موجودی با سفارش/ویرایش دیگری تغییر کرده؛ پیش‌نمایش تازه را تأیید کنید', ['status'=>409]);
        }
        return true;
    }

    public static function committed($product, $request, $creating) {
        global $wpdb;
        if (self::$transaction) {
            if ($wpdb->query('COMMIT') === false) throw new RuntimeException('Tisa inventory commit was not acknowledged');
            self::$transaction = false;
        }
    }
}
