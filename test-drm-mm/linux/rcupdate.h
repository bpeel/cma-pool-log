#ifndef __LINUX_RCUPDATE_H
#define __LINUX_RCUPDATE_H

#define rcu_assign_pointer(dst, src) do { (dst) = (src); } while (0)

#endif /* __LINUX_RCUPDATE_H */
