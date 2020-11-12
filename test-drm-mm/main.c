#include <stdio.h>
#include <stdlib.h>
#include <drm/drm_mm.h>
#include <stdint.h>
#include <inttypes.h>
#include <string.h>
#include <ctype.h>

/* Limit the amount of CMA memory allocated to 128MB */
#define VC4_CMA_POOL_SIZE (128 * 1024 * 1024)

enum madv {
        MADV_WILLNEED,
        MADV_DONTNEED,
};

struct buffer {
        struct drm_mm_node mm_node;

        /* Position in the list of all buffers */
        struct list_head all_buffers_head;

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
free_buffer(struct buffer *buf)
{
        if (buf->paged_in)
                drm_mm_remove_node(&buf->mm_node);

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

static void
page_in_buffer(struct data *data,
               struct buffer *buf)
{
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

        drm_mm_init(&data.mm,
                    0, /* start */
                    VC4_CMA_POOL_SIZE);

        process_file(&data, stdin);

        free_buffers(&data);

        drm_mm_takedown(&data.mm);

        return EXIT_SUCCESS;
}
