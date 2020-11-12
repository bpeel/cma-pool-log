#include <stdio.h>
#include <stdlib.h>
#include <drm/drm_mm.h>
#include <stdint.h>
#include <inttypes.h>
#include <string.h>
#include <ctype.h>
#include <assert.h>

/* Limit the amount of CMA memory allocated to 128MB */
#define VC4_CMA_POOL_SIZE (128 * 1024 * 1024)

#define PAGE_SHIFT 12
#define PAGE_SIZE (1UL << PAGE_SHIFT)

enum madv {
        MADV_WILLNEED,
        MADV_DONTNEED,
};

struct buffer {
        struct drm_mm_node mm_node;

        /* Position in the list of all buffers */
        struct list_head all_buffers_head;
        /* Position in the MRU list. The buffer will only be in the
         * list if paged_in is true.
         */
        struct list_head mru_buffers_head;
        /* Temporary list node used for choosing what to evict */
        struct list_head eviction_head;

        /* The address of the buffer object that appeared in the log.
         * We’ll use this as an identifier to find the object again.
         */
        uint32_t name;

        bool paged_in;
        bool unmoveable;
        bool in_use;

        enum madv madv;

        size_t size;
};

struct data {
        struct list_head all_buffers;
        /* Buffers in order of most-recently-used first. Buffers will
         * only be in this list if they are paged in.
         */
        struct list_head mru_buffers;
        struct drm_mm mm;
        int line_num;
};

static struct buffer *
find_buffer(struct data *data,
            uint32_t name)
{
        struct buffer *buf;

        list_for_each_entry(buf, &data->all_buffers, all_buffers_head) {
                if (buf->name == name)
                        return buf;
        }

        return NULL;
}

static struct buffer *
find_buffer_or_error(struct data *data,
                     uint32_t name)
{
        struct buffer *buf = find_buffer(data, name);

        if (buf == NULL) {
                fprintf(stderr,
                        "line %i: unknown buffer %08" PRIu32 "\n",
                        data->line_num,
                        name);
        }

        return buf;
}

static void
remove_buffer_from_pool(struct buffer *buf)
{
        drm_mm_remove_node(&buf->mm_node);
        list_del(&buf->mru_buffers_head);
        buf->paged_in = false;
}

static void
free_buffer(struct buffer *buf)
{
        if (buf->paged_in)
                remove_buffer_from_pool(buf);

        list_del(&buf->all_buffers_head);

        free(buf);
}

static void
free_buffers(struct data *data)
{
        struct buffer *buf, *tmp;

        list_for_each_entry_safe(buf, tmp,
                                 &data->all_buffers,
                                 all_buffers_head) {
                free_buffer(buf);
        }
}

static bool
page_out_buffers_for_insertion(struct data *data,
                               size_t size)
{
        struct buffer *buffer, *tmp;
        struct list_head eviction_list;
        struct drm_mm_scan scan;
        int ret;

        drm_mm_scan_init_with_range(&scan,
                                    &data->mm,
                                    size,
                                    PAGE_SIZE,
                                    0, /* color */
                                    0, /* start */
                                    VC4_CMA_POOL_SIZE,
                                    DRM_MM_INSERT_BEST);
        INIT_LIST_HEAD(&eviction_list);

        /* Let the drm_mm pick what to evict to make a hole for the
         * buffer. The buffers are scanned in order of LRU so that it
         * will hopefully prefer paging out unused buffers first.
         */
        list_for_each_entry_safe_reverse(buffer,
                                         tmp,
                                         &data->mru_buffers,
                                         mru_buffers_head) {
                /* Don’t page out buffers that are in use or unmoveable */
                if (buffer->in_use || buffer->unmoveable)
                        continue;

                list_add(&buffer->eviction_head, &eviction_list);

                if (drm_mm_scan_add_block(&scan, &buffer->mm_node))
                        goto found;
        }

        /* Nothing found, clean up and bail out! */
        list_for_each_entry(buffer, &eviction_list, eviction_head) {
                ret = drm_mm_scan_remove_block(&scan, &buffer->mm_node);
                BUG_ON(ret);
        }

        return false;

found:
        /* drm_mm doesn’t allow any other other operations while
         * scanning, so we’ll do this in two steps by removing
         * anything that shouldn’t be evicted from the list and then
         * paging them all out as the second step.
         */
        list_for_each_entry_safe(buffer, tmp, &eviction_list, eviction_head) {
                if (!drm_mm_scan_remove_block(&scan, &buffer->mm_node))
                        list_del(&buffer->eviction_head);
        }

        list_for_each_entry(buffer, &eviction_list, eviction_head) {
                remove_buffer_from_pool(buffer);
        }

        return true;
}

static void
userspace_cache_purge(struct data *data)
{
        struct buffer *buf, *tmp;

        list_for_each_entry_safe(buf,
                                 tmp,
                                 &data->mru_buffers,
                                 mru_buffers_head) {
                if (buf->madv == MADV_DONTNEED &&
                    !buf->in_use &&
                    !buf->unmoveable)
                        remove_buffer_from_pool(buf);
        }
}

static bool
insert_buffer_in_cma_pool(struct data *data,
                          struct buffer *buf,
                          enum drm_mm_insert_mode mode)
{
        int ret;

        ret = drm_mm_insert_node_generic(&data->mm,
                                         &buf->mm_node,
                                         buf->size,
                                         PAGE_SIZE,
                                         0, /* color */
                                         mode);

        if (ret)
                return false;

        list_add(&buf->mru_buffers_head, &data->mru_buffers);
        buf->paged_in = true;

        return true;
}

static bool
page_in_buffer(struct data *data,
               struct buffer *buf)
{
        /* Check if there is a gap already available */
        if (insert_buffer_in_cma_pool(data, buf, DRM_MM_INSERT_BEST))
                return true;

        /*
         * Not enough CMA memory in the pool, purge the userspace BO
         * cache and retry.
         * This is sub-optimal since we purge the whole userspace BO
         * cache which forces user that want to re-use the BO to
         * restore its initial content.
         * Ideally, we should purge entries one by one and retry after
         * each to see if CMA allocation succeeds. Or even better, try
         * to find an entry with at least the same size.
         */
        userspace_cache_purge(data);

        if (insert_buffer_in_cma_pool(data, buf, DRM_MM_INSERT_BEST))
                return true;

        /* Try paging out some unused buffers */
        if (page_out_buffers_for_insertion(data, buf->size) &&
            insert_buffer_in_cma_pool(data, buf, DRM_MM_INSERT_EVICT))
                return true;

        fprintf(stderr,
                "Couldn't find insertion point for buffer of size %zu\n",
                buf->size);

        return false;
}

static bool
check_no_args(struct data *data,
              const char *args)
{
        if (*args) {
                fprintf(stderr,
                        "line %i: unexpected args\n",
                        data->line_num);
                return true;
        }

        return false;
}

static void
buf_destroy(struct data *data,
            uint32_t buf_name,
            const char *args)
{
        if (check_no_args(data, args))
                return;

        struct buffer *buf = find_buffer_or_error(data, buf_name);
        if (buf == NULL)
                return;

        free_buffer(buf);
}

static void
buf_add_usecnt(struct data *data,
               uint32_t buf_name,
               const char *args)
{
        if (check_no_args(data, args))
                return;

        struct buffer *buf = find_buffer_or_error(data, buf_name);
        if (buf == NULL)
                return;

        if (buf->in_use) {
                fprintf(stderr,
                        "line %i: add_usecnt on buffer %08" PRIu32 " but "
                        "buffer already in use\n",
                        data->line_num,
                        buf_name);
        }

        buf->in_use = true;
}

static void
buf_remove_usecnt(struct data *data,
                  uint32_t buf_name,
                  const char *args)
{
        if (check_no_args(data, args))
                return;

        struct buffer *buf = find_buffer_or_error(data, buf_name);
        if (buf == NULL)
                return;

        if (!buf->in_use) {
                fprintf(stderr,
                        "line %i: remove_usecnt on buffer %08" PRIu32 " but "
                        "buffer not in use\n",
                        data->line_num,
                        buf_name);
        }

        buf->in_use = false;
}

static void
buf_create(struct data *data,
           uint32_t buf_name,
           const char *args)
{
        struct buffer *buf = find_buffer(data, buf_name);

        if (buf) {
                fprintf(stderr,
                        "line %i: creating buffer %08" PRIx32 " but buffer "
                        "already exists\n",
                        data->line_num,
                        buf_name);
                return;
        }

        size_t buf_size;
        int args_offset;
        int ret;

        ret = sscanf(args,
                     "%zu%n",
                     &buf_size,
                     &args_offset);

        if (ret != 1) {
                fprintf(stderr,
                        "line %i: bad buffer size\n",
                        data->line_num);
                return;
        }

        bool unmoveable = false;

        while (args[args_offset]) {
                if (!isspace(args[args_offset++])) {
                        unmoveable = true;
                        break;
                }
        }

        buf = calloc(sizeof *buf, 1);
        buf->name = buf_name;
        buf->size = buf_size;
        buf->madv = MADV_WILLNEED;
        buf->unmoveable = unmoveable;
        list_add(&buf->all_buffers_head, &data->all_buffers);

        page_in_buffer(data, buf);
}

static void
buf_use(struct data *data,
        uint32_t buf_name,
        const char *args)
{
        if (check_no_args(data, args))
                return;

        struct buffer *buf = find_buffer_or_error(data, buf_name);
        if (buf == NULL)
                return;

        if (buf->paged_in) {
                /* Move the buffer to the head of the MRU list */
                list_del(&buf->mru_buffers_head);
                list_add(&buf->mru_buffers_head, &data->mru_buffers);
        } else {
                page_in_buffer(data, buf);
        }
}

static void
buf_madv(struct data *data,
         uint32_t buf_name,
         const char *args)
{
        struct buffer *buf = find_buffer_or_error(data, buf_name);
        if (buf == NULL)
                return;

        const char *args_end = args;

        while (*args_end && !isspace(*args_end))
                args_end++;

        static const struct {
                const char *name;
                enum madv value;
        } madv_map[] = {
                { "willneed", MADV_WILLNEED },
                { "dontneed", MADV_DONTNEED },
                { NULL },
        };

        for (int i = 0; madv_map[i].name; i++) {
                size_t name_len = strlen(madv_map[i].name);
                if (name_len == args_end - args &&
                    !memcmp(madv_map[i].name, args, args_end - args))
                        buf->madv = madv_map[i].value;
                return;
        }

        fprintf(stderr,
                "line %i: invalid madv value %*s\n",
                data->line_num,
                (int) (args_end - args),
                args);
}

static void
process_line(struct data *data,
             const char *line)
{
        static const char command_marker[] = "] @@@ ";
        const char *marker_pos = strstr(line, command_marker);

        if (marker_pos == NULL)
                return;

        const char *command_name = marker_pos + (sizeof command_marker) - 1;
        const char *command_end = strchr(command_name, ' ');

        if (command_end == NULL)
                return;

        const char *args = command_end + 1;
        uint32_t buf_name;
        int args_offset;
        int ret;

        ret = sscanf(args,
                     "%" SCNx32 "%n",
                     &buf_name,
                     &args_offset);

        if (ret != 1) {
                fprintf(stderr, "invalid address on line %i\n", data->line_num);
                return;
        }

        while (isspace(args[args_offset]))
                args_offset++;

        static const struct {
                const char *name;
                void (* func)(struct data *data,
                              uint32_t buf_name,
                              const char *args);
        } commands[] = {
                { "destroy", buf_destroy },
                { "add_usecnt", buf_add_usecnt },
                { "remove_usecnt", buf_remove_usecnt },
                { "create", buf_create },
                { "use", buf_use },
                { "madv", buf_madv },
                { NULL },
        };

        for (int i = 0; commands[i].name; i++) {
                size_t name_len = strlen(commands[i].name);

                if (name_len == command_end - command_name &&
                    !memcmp(command_name, commands[i].name, name_len)) {
                        commands[i].func(data,
                                         buf_name,
                                         args + args_offset);
                        return;
                }
        }

        fprintf(stderr,
                "unknown command %*s on line %i\n",
                (int) (command_end - command_name),
                command_name,
                data->line_num);
}

static void
process_file(struct data *data,
             FILE *file)
{
        char line[1024];

        data->line_num = 1;

        while (fgets(line, sizeof line, file)) {
                process_line(data, line);
                data->line_num++;
        }
}

int
main(int argc, char **argv)
{
        struct data data;

        INIT_LIST_HEAD(&data.all_buffers);
        INIT_LIST_HEAD(&data.mru_buffers);

        drm_mm_init(&data.mm,
                    0, /* start */
                    VC4_CMA_POOL_SIZE);

        process_file(&data, stdin);

        free_buffers(&data);

        drm_mm_takedown(&data.mm);

        return EXIT_SUCCESS;
}
