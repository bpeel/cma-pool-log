#ifndef _LINUX_KERNEL_H
#define _LINUX_KERNEL_H

#include <stdbool.h>
#include <stdint.h>

#define WRITE_ONCE(dst, src) do { (dst) = (src); } while (0)
#define READ_ONCE(src) (src)

#define container_of(ptr, type, member) \
        ((type *)((ptr) - offsetof(type, member)))

#define EXPORT_SYMBOL(x)

#define likely(x) (x)
#define unlikely(x) (x)

static inline __attribute__((const))
bool is_power_of_2(unsigned long n)
{
	return (n != 0 && ((n & (n - 1)) == 0));
}

#define min(a, b) ((a) < (b) ? (a) : (b))
#define max(a, b) ((a) > (b) ? (a) : (b))

static inline uint64_t div64_u64_rem(uint64_t dividend,
                                     uint64_t divisor,
                                     uint64_t *remainder)
{
	*remainder = dividend % divisor;
	return dividend / divisor;
}

#endif
