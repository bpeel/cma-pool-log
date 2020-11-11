#ifndef DRM_PRINT_H
#define DRM_PRINT_H

#include <stdio.h>
#include <inttypes.h>

struct drm_printer {
        int stub;
};

#define drm_printf(p, format...) fprintf(stderr, format)

#endif
