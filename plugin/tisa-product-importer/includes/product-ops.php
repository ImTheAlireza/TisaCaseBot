<?php
/** Validated, transactional Woo CRUD shared by ZIP create and ZIP restock. */
if (!defined('ABSPATH')) exit;

final class Tisa_Product_Operations {
    const MAX_VARIATIONS = 1000;

    private static function number($value, $label, $zero = true) {
        if (is_bool($value) || !is_numeric($value) || !is_finite((float)$value) || (float)$value !== (float)(int)$value) {
            throw new RuntimeException($label . ' باید عدد صحیح باشد');
        }
        $value = (int)$value;
        if ($value < ($zero ? 0 : 1000) || $value > 500000000) throw new RuntimeException($label . ' خارج از بازهٔ ایمن است');
        return $value;
    }

    private static function values($values) {
        if (!is_array($values)) throw new RuntimeException('گزینه‌های ویژگی باید فهرست باشند');
        $out = [];
        foreach ($values as $value) {
            if (!is_string($value) || mb_strlen($value) > 160) throw new RuntimeException('گزینهٔ ویژگی نامعتبر/بسیار بلند است');
            $value = sanitize_text_field($value);
            if ($value !== '' && !in_array($value, $out, true)) $out[] = $value;
        }
        if (count($out) > self::MAX_VARIATIONS) throw new RuntimeException('تعداد گزینه‌ها از سقف ایمن بیشتر است');
        return $out;
    }

    public static function normalize($data) {
        if (!is_array($data)) throw new RuntimeException('ساختار product.json نامعتبر است');
        $data['title'] = sanitize_text_field($data['title'] ?? '');
        $data['sku_prefix'] = strtoupper((string)($data['sku_prefix'] ?? ''));
        if ($data['sku_prefix'] !== '' && !preg_match('/^[A-Z][A-Z0-9]{0,11}$/D', $data['sku_prefix'])) throw new RuntimeException('پیشوند SKU نامعتبر است');
        $data['models'] = self::values($data['models'] ?? []);
        $attrs = [];
        foreach ((array)($data['attributes'] ?? []) as $name => $values) {
            if (!is_string($name) || mb_strlen($name) > 80 || !sanitize_text_field($name)) throw new RuntimeException('نام ویژگی نامعتبر است');
            $name = Tisa_REST_Fencing::key($name) === 'airpods' ? 'ایرپاد' : sanitize_text_field($name);
            $attrs[$name] = self::values($values);
        }
        $phones = []; $pods = $attrs['ایرپاد'] ?? [];
        foreach ($data['models'] as $model) {
            if (preg_match('/^(?:apple\s*)?air\s*pods?\s*/i', $model)) {
                $tail = preg_replace('/^(?:apple\s*)?air\s*pods?\s*/i', '', $model);
                $tail = preg_replace('/pro\s*([1-3])/i', 'Pro $1', $tail);
                $tail = preg_replace('/\bpro\b/i', 'Pro', $tail);
                $pods[] = 'AirPods ' . trim($tail);
            } else $phones[] = $model;
        }
        $pods = array_values(array_unique($pods));
        if ($phones && $pods) { $data['models'] = $phones; $attrs['ایرپاد'] = $pods; }
        elseif ($pods) { $data['models'] = $pods; unset($attrs['ایرپاد']); }
        $data['attributes'] = $attrs;
        foreach (['price','sale_price','wholesale_price'] as $field) {
            $value = $data[$field] ?? 0;
            $data[$field] = self::number($value, $field, true);
            if ($data[$field] > 0 && $data[$field] < 1000) throw new RuntimeException('قیمت کمتر از حد ایمن است');
        }
        foreach (['prices','model_prices'] as $field) {
            $out = [];
            foreach ((array)($data[$field] ?? []) as $name => $amount) $out[(string)$name] = self::number($amount, 'قیمت مدل/گروه', false);
            $data[$field] = $out;
        }
        if (isset($data['stock'])) $data['stock'] = self::number($data['stock'], 'موجودی');
        $data['stock_status'] = (string)($data['stock_status'] ?? '');
        if ($data['stock_status'] && !in_array($data['stock_status'], ['instock','outofstock','onbackorder'], true)) throw new RuntimeException('وضعیت موجودی نامعتبر است');
        if (!empty($data['pricing_errors']) || !empty($data['stock_matrix_errors'])) throw new RuntimeException('جدول قیمت/موجودی هنوز خطای حل‌نشده دارد');
        foreach ((array)($data['stock_matrix'] ?? []) as $design => $row) {
            if (!is_array($row)) throw new RuntimeException('ردیف ماتریس موجودی نامعتبر است');
            foreach ($row as $model => $quantity) if ($quantity !== null) $data['stock_matrix'][$design][$model] = self::number($quantity, 'خانهٔ موجودی');
        }
        if (isset($data['stock']) && !empty($data['stock_matrix'])) throw new RuntimeException('موجودی کلی و ماتریسی هم‌زمان مجاز نیست');
        $data['categories'] = self::values($data['categories'] ?? []);
        return $data;
    }

    public static function axes($data, $existing = null) {
        $axes = [];
        if ($existing) foreach ($existing->get_attributes() as $attribute) {
            if (!$attribute->get_variation()) continue;
            $name = wc_attribute_label($attribute->get_name(), $existing);
            $options = $attribute->is_taxonomy() ? wc_get_product_terms($existing->get_id(), $attribute->get_name(), ['fields'=>'names']) : $attribute->get_options();
            $axes[$name] = self::values($options);
        }
        $mixed = !empty($data['models']) && !empty($data['attributes']['ایرپاد']);
        $incoming = $data['attributes'];
        if ($data['models']) $incoming = array_merge(['مدل'=>$data['models']], $incoming);
        foreach ($incoming as $name => $values) {
            $key = Tisa_REST_Fencing::key($name); $matched = null;
            foreach (array_keys($axes) as $old_name) if (Tisa_REST_Fencing::key($old_name) === $key) { $matched = $old_name; break; }
            if ($matched !== null) { $axes[$matched] = $values; continue; }
            if (count($values) >= 2 || $mixed && in_array($key, ['model','airpods'], true)) $axes[$name] = $values;
        }
        if (count($axes) > 8) throw new RuntimeException('بیش از هشت محور واریژن مجاز نیست');
        return $axes;
    }

    private static function allowed($combo, $restrictions) {
        $limits = [];
        foreach ((array)$restrictions as $model => $colors) $limits[Tisa_REST_Fencing::option('مدل', $model)] = array_map(function($color) { return Tisa_REST_Fencing::option('رنگ', $color); }, (array)$colors);
        foreach ($combo as $name => $value) {
            if (!in_array(Tisa_REST_Fencing::key($name), ['model','airpods'], true)) continue;
            $model = Tisa_REST_Fencing::option($name, $value);
            if (!array_key_exists($model, $limits)) continue;
            foreach ($combo as $color_name => $color) if (Tisa_REST_Fencing::key($color_name) === 'color' && !in_array(Tisa_REST_Fencing::option($color_name, $color), $limits[$model], true)) return false;
        }
        return true;
    }

    public static function combinations($axes, $data) {
        $combos = [[]];
        foreach ($axes as $name => $values) {
            $next = [];
            foreach ($combos as $combo) foreach ($values as $value) {
                $candidate = $combo + [$name=>$value];
                if (!self::allowed($candidate, $data['model_colors'] ?? [])) continue;
                $next[] = $candidate;
                if (count($next) > self::MAX_VARIATIONS) throw new RuntimeException('بیش از ۱۰۰۰ ترکیب؛ محصول را تقسیم کنید');
            }
            $combos = $next;
        }
        if (!$combos) throw new RuntimeException('هیچ ترکیب سازگار مدل/رنگ باقی نمانده است');
        if (!empty($data['stock_matrix'])) {
            foreach ($axes as $name => $values) if (!in_array(Tisa_REST_Fencing::key($name), ['model','طرح'], true)) throw new RuntimeException('ماتریس موجودی فقط مدل × طرح را پشتیبانی می‌کند');
            $kept = [];
            foreach ($combos as $combo) if (self::matrix_quantity($data, $combo) !== null) $kept[] = $combo;
            $combos = $kept;
            if (!$combos) throw new RuntimeException('ماتریس هیچ ترکیب عددی ندارد');
        }
        return $combos;
    }

    public static function matrix_quantity($data, $combo) {
        $matrix = $data['stock_matrix'] ?? [];
        $designs = array_keys($matrix); $models = $data['models'];
        $design = $combo['طرح'] ?? (count($designs) === 1 ? $designs[0] : '');
        $model = ''; foreach ($combo as $name=>$value) if (Tisa_REST_Fencing::key($name) === 'model') $model = $value;
        if (!$model && count($models) === 1) $model = $models[0];
        if (!array_key_exists($design, $matrix) || !array_key_exists($model, $matrix[$design])) throw new RuntimeException('خانهٔ مدل × طرح در ماتریس موجودی مشخص نشده است');
        return $matrix[$design][$model];
    }

    private static function model($combo, $data) {
        foreach ($combo as $name=>$value) if (Tisa_REST_Fencing::key($name) === 'model') return $value;
        return count($data['models']) === 1 ? $data['models'][0] : '';
    }

    public static function price($data, $combo, $fallback = 0) {
        $model = self::model($combo, $data);
        $wanted = Tisa_REST_Fencing::option('مدل', $model);
        foreach ($data['model_prices'] as $name=>$amount) if (Tisa_REST_Fencing::option('مدل', $name) === $wanted) return $amount;
        $group = preg_match('/iphone|آیفون|ایفون/i', $model) ? 'iphone' : 'android';
        return $data['prices'][$group] ?? ($data['price'] ?: $fallback);
    }

    private static function stock($product, $data, $combo) {
        if (!empty($data['stock_matrix'])) $quantity = self::matrix_quantity($data, $combo);
        else $quantity = $data['stock'] ?? null;
        if ($quantity !== null) { $product->set_manage_stock(true); $product->set_stock_quantity($quantity); }
        $status = $data['stock_status'] ?: ($quantity !== null ? ($quantity > 0 ? 'instock' : 'outofstock') : '');
        if ($status) $product->set_stock_status($status);
    }

    private static function prices($product, $data, $combo, $fallback = 0) {
        $price = self::price($data, $combo, $fallback);
        if ($price <= 0) throw new RuntimeException('برای یک ترکیب قیمت معتبر پیدا نشد');
        $sale = $data['sale_price'] ?: (float)$product->get_sale_price('edit');
        if ($sale && $sale >= $price) throw new RuntimeException('قیمت ویژه باید از قیمت عادی همین ترکیب کمتر باشد');
        $product->set_regular_price((string)$price);
        if ($data['sale_price']) $product->set_sale_price((string)$data['sale_price']);
    }

    private static function attributes($axes, $product) {
        $old = $product->get_attributes(); $all = [];
        foreach ($old as $attribute) if (!$attribute->get_variation()) $all[] = $attribute;
        foreach ($axes as $name=>$values) {
            $found = null;
            foreach ($old as $attribute) if (Tisa_REST_Fencing::key(wc_attribute_label($attribute->get_name(), $product)) === Tisa_REST_Fencing::key($name)) { $found = clone $attribute; break; }
            if ($found && !$found->get_variation()) throw new RuntimeException('ویژگی موجود برای واریژن فعال نیست؛ ابتدا در پیشخوان فعال کنید');
            $attribute = $found ?: new WC_Product_Attribute();
            if (!$found) { $attribute->set_id(0); $attribute->set_name($name); $attribute->set_visible(true); }
            if ($attribute->is_taxonomy()) {
                $ids = [];
                foreach ($values as $value) {
                    $term = term_exists($value, $attribute->get_name());
                    if (!$term) $term = wp_insert_term($value, $attribute->get_name());
                    if (is_wp_error($term)) throw new RuntimeException($term->get_error_message());
                    $ids[] = (int)(is_array($term) ? $term['term_id'] : $term);
                }
                $attribute->set_options($ids);
            } else $attribute->set_options($values);
            $attribute->set_variation(true); $attribute->set_position(count($all)); $all[] = $attribute;
        }
        $product->set_attributes($all);
    }

    private static function variation_attributes($combo, $product) {
        $attrs = [];
        foreach ($combo as $name=>$value) {
            $attribute_name = $name; $option = $value;
            foreach ($product->get_attributes() as $attribute) if (Tisa_REST_Fencing::key(wc_attribute_label($attribute->get_name(), $product)) === Tisa_REST_Fencing::key($name)) {
                $attribute_name = $attribute->get_name();
                if ($attribute->is_taxonomy()) {
                    $term = get_term_by('name', $value, $attribute_name);
                    if (!$term) throw new RuntimeException('گزینهٔ taxonomy پیدا نشد');
                    $option = $term->slug;
                }
                break;
            }
            $attrs[sanitize_title($attribute_name)] = $option; // NOT sanitize_key: Persian survives.
        }
        return $attrs;
    }

    public static function children($id) {
        global $wpdb;
        return $wpdb->get_col($wpdb->prepare("SELECT ID FROM {$wpdb->posts} WHERE post_parent=%d AND post_type='product_variation' AND post_status<>'trash' ORDER BY ID LIMIT 5001", $id));
    }

    public static function snapshot($id) {
        global $wpdb;
        $rows = $wpdb->get_results($wpdb->prepare(
            "SELECT p.ID,p.post_title,p.post_content,p.post_status,p.post_modified_gmt,m.meta_key,m.meta_value FROM {$wpdb->posts} p " .
            "LEFT JOIN {$wpdb->postmeta} m ON p.ID=m.post_id WHERE (p.ID=%d OR (p.post_parent=%d AND p.post_type='product_variation')) " .
            "AND (m.meta_key IS NULL OR m.meta_key IN ('_regular_price','_sale_price','_stock','_manage_stock','_stock_status','_sku','_product_attributes','_thumbnail_id','_product_image_gallery','_tax_class','_backorders','_weight','_height','_width','_length')) " .
            "ORDER BY p.ID,m.meta_key,m.meta_id", $id, $id), ARRAY_A);
        if (!$rows) throw new RuntimeException('محصول پیدا نشد');
        $json = wp_json_encode($rows);
        if (strlen($json) > 2097152) throw new RuntimeException('اطلاعات محصول برای پیش‌نمایش امن بسیار بزرگ است');
        return hash('sha256', $json);
    }

    private static function begin($id = 0) {
        global $wpdb;
        if (!Tisa_REST_Fencing::transactional()) throw new RuntimeException('عملیات امن به جدول‌های InnoDB نیاز دارد');
        if ($id && !Tisa_REST_Fencing::lock('product:' . $id)) throw new RuntimeException('قفل محصول در دسترس نیست');
        if ($wpdb->query('START TRANSACTION') === false) throw new RuntimeException('شروع تراکنش ناموفق بود');
        if ($id) {
            $ids = array_map('intval', array_merge([$id], self::children($id)));
            if (count($ids) > 5001) throw new RuntimeException('محصول بیش از سقف ایمن واریژن دارد');
            $wpdb->get_results("SELECT meta_id FROM {$wpdb->postmeta} WHERE post_id IN (" . implode(',', $ids) . ") FOR UPDATE");
            foreach ($ids as $pid) clean_post_cache($pid);
        }
    }

    public static function next_sku($prefix) {
        global $wpdb;
        $rows = $wpdb->get_col($wpdb->prepare("SELECT meta_value FROM {$wpdb->postmeta} WHERE meta_key='_sku' AND meta_value LIKE %s", $wpdb->esc_like($prefix) . '%'));
        $maximum = 0;
        foreach ($rows as $sku) if (preg_match('/^' . preg_quote($prefix, '/') . '([0-9]+)$/D', $sku, $match)) $maximum = max($maximum, (int)$match[1]);
        return $prefix . ($maximum + 1);
    }

    public static function categories($paths) {
        $ids = [];
        foreach ($paths as $path) {
            $parts = array_values(array_filter(array_map('trim', explode('>', str_replace('&gt;', '>', $path)))));
            if (count($parts) === 1) {
                $leaf = $parts[0]; $guard = 0;
                while (isset(Tisa_Product_Zip_Importer::CATEGORY_PARENTS[$leaf]) && $guard++ < 10) { $leaf = Tisa_Product_Zip_Importer::CATEGORY_PARENTS[$leaf]; array_unshift($parts, $leaf); }
            }
            $parent = 0;
            foreach ($parts as $part) {
                $term = term_exists($part, 'product_cat', $parent);
                if (!$term) $term = wp_insert_term($part, 'product_cat', ['parent'=>$parent]);
                if (is_wp_error($term)) throw new RuntimeException($term->get_error_message());
                $parent = (int)(is_array($term) ? $term['term_id'] : $term); $ids[] = $parent;
            }
        }
        return array_values(array_unique($ids));
    }

    private static function color_image($combo, $images) {
        foreach ($combo as $name=>$value) if (Tisa_REST_Fencing::key($name) === 'color') {
            $wanted = Tisa_REST_Fencing::option('رنگ', $value);
            foreach ($images as $image) if ($wanted && strpos(Tisa_REST_Fencing::option('رنگ', pathinfo($image['name'], PATHINFO_FILENAME)), $wanted) !== false) return $image['id'];
        }
        return 0;
    }

    private static function grid($product, $axes, $data, $combos, $images) {
        $existing = []; $pool = [];
        foreach (self::children($product->get_id()) as $id) {
            $variation = wc_get_product($id); if (!$variation) continue;
            $key = wp_json_encode(Tisa_REST_Fencing::combo($variation->get_attributes(), $product));
            $existing[$key][] = $variation;
            if ((float)$variation->get_regular_price('edit') > 0) $pool[] = (float)$variation->get_regular_price('edit');
        }
        $fallback = $pool ? (float)array_keys(array_count_values(array_map('strval', $pool)), max(array_count_values(array_map('strval', $pool))))[0] : (float)$product->get_regular_price('edit');
        foreach ($combos as $order=>$combo) {
            $attributes = self::variation_attributes($combo, $product);
            $key = wp_json_encode(Tisa_REST_Fencing::combo($attributes, $product));
            $variation = !empty($existing[$key]) ? array_shift($existing[$key]) : new WC_Product_Variation();
            $new = !$variation->get_id();
            if ($new) { $variation->set_parent_id($product->get_id()); $variation->set_status('publish'); $variation->set_attributes($attributes); }
            self::prices($variation, $data, $combo, (float)$variation->get_regular_price('edit') ?: $fallback);
            self::stock($variation, $data, $combo);
            $image = self::color_image($combo, $images); if ($image) $variation->set_image_id($image);
            $variation->set_menu_order($order);
            if (!$variation->save()) throw new RuntimeException('ذخیرهٔ واریژن تأیید نشد');
        }
        // All desired rows were saved. Only removed combinations/twins go away;
        // common IDs keep SKU/tax/weight/backorders/description/custom metadata.
        foreach ($existing as $twins) foreach ($twins as $variation) if (!$variation->delete(true)) throw new RuntimeException('حذف واریژن تأیید نشد');
        WC_Product_Variable::sync($product->get_id());
    }

    public static function create($data, $images, $batch) {
        global $wpdb;
        $data = self::normalize($data);
        if (!$data['title'] || !$data['sku_prefix'] || !$images) throw new RuntimeException('عنوان، پیشوند SKU و تصاویر لازم است');
        $axes = self::axes($data); $combos = self::combinations($axes, $data);
        if (!Tisa_REST_Fencing::lock('batch:' . $batch) || !Tisa_REST_Fencing::lock('sku:all')) throw new RuntimeException('قفل انتشار/SKU در دسترس نیست');
        $existing = Tisa_REST_Fencing::find_batch($batch);
        if ($existing) return $existing;
        $id = 0;
        try {
            self::begin();
            $product = $axes ? new WC_Product_Variable() : new WC_Product_Simple();
            $product->set_name($data['title']); $product->set_status('draft'); $product->set_description('');
            $product->set_sku(self::next_sku($data['sku_prefix']));
            $product->add_meta_data('tisa_batch_id', $batch, true);
            $product->set_category_ids(self::categories($data['categories']));
            $product->set_image_id($images[0]['id']); $product->set_gallery_image_ids(array_column(array_slice($images, 1), 'id'));
            if ($axes) self::attributes($axes, $product);
            else { self::prices($product, $data, []); self::stock($product, $data, []); }
            $id = $product->save(); if (!$id) throw new RuntimeException('ذخیرهٔ محصول تأیید نشد');
            if ($axes) self::grid($product, $axes, $data, $combos, $images);
            if ($wpdb->query('COMMIT') === false) throw new RuntimeException('پایان تراکنش تأیید نشد');
            wc_delete_product_transients($id); return $id;
        } catch (Throwable $error) {
            $wpdb->query('ROLLBACK'); if ($id) clean_post_cache($id);
            throw $error;
        }
    }

    public static function update($id, $data, $images, $snapshot) {
        global $wpdb;
        $data = self::normalize($data);
        if (!empty($data['stock_matrix'])) throw new RuntimeException('ماتریس موجودی در اپدیت پشتیبانی نمی‌شود');
        try {
            self::begin($id);
            if (!hash_equals(self::snapshot($id), (string)$snapshot)) throw new RuntimeException('قیمت/موجودی/گزینه‌ها پس از انتخاب تغییر کرده؛ دوباره جستجو و تأیید کنید');
            $product = wc_get_product($id);
            if (!$product || !in_array($product->get_type(), ['simple','variable'], true)) throw new RuntimeException('نوع محصول برای این عملیات پشتیبانی نمی‌شود');
            $axes = self::axes($data, $product); $combos = self::combinations($axes, $data);
            if ($data['title']) $product->set_name($data['title']);
            if ($images) { $product->set_image_id($images[0]['id']); $product->set_gallery_image_ids(array_column(array_slice($images, 1), 'id')); }
            if ($axes) self::attributes($axes, $product);
            else { self::prices($product, $data, [], (float)$product->get_regular_price('edit')); self::stock($product, $data, []); }
            if (!$product->save()) throw new RuntimeException('ذخیرهٔ محصول تأیید نشد');
            if ($axes) self::grid($product, $axes, $data, $combos, $images);
            if ($wpdb->query('COMMIT') === false) throw new RuntimeException('پایان تراکنش تأیید نشد');
            wc_delete_product_transients($id);
        } catch (Throwable $error) { $wpdb->query('ROLLBACK'); clean_post_cache($id); wc_delete_product_transients($id); throw $error; }
    }
}
