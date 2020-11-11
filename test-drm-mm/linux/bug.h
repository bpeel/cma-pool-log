#ifndef __ASM_BUG_H
#define __ASM_BUG_H

#include <assert.h>
#include <stdio.h>

#define BUG_ON(x) assert(!(x))
#define BUILD_BUG_ON_INVALID(x) BUG_ON(x)

#define WARN(condition, format...) ({                           \
                        int __ret_warn_on = !!(condition);      \
                        if (__ret_warn_on)                      \
                                fprintf(stderr, format);        \
                        __ret_warn_on;                          \
                })

#endif /* __ASM_BUG_H */
