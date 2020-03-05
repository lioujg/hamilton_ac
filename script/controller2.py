#!/usr/bin/python3
import numpy as np
from numpy import sin, cos
from geometry_msgs.msg import Twist,Vector3,PoseStamped
from std_msgs.msg import Bool, Float64MultiArray, Float64
from hamilton_ac.msg import Reference
import rospy

class AdaptiveController():
    #implements adaptive controller for ouijabot in 2D manipulation
    #a = [m,J,m*rpx,m*rpy,u1,u1*rix,u1*riy,u1*||ri||,rix,riy]
    def __init__(self):
        self.controllerReset()
        self.getParams()
        self.active = False
        self.q = self.q_raw = self.dq = np.zeros(3)
        self.q_des, self.dq_des = np.zeros(3), np.zeros(3)
        self.ddq_des = np.zeros(3)
        self.state_time = -1
        self.q_prev = np.zeros(3)

        self.cmd_pub = rospy.Publisher('cmd_global',Twist,queue_size=1)
        self.state_sub = rospy.Subscriber('state',PoseStamped,
            self.stateCallback)

        self.ref_sub = rospy.Subscriber('/ac/ref',Reference,self.refCallback)
        self.active_sub = rospy.Subscriber('/ac/active', Bool,
            self.activeCallback)
        self.state_pub = rospy.Publisher('state_est',Reference,queue_size=1)
        self.err_pub = rospy.Publisher('err', Reference, queue_size=1)
        self.e_norm_pub = rospy.Publisher('e_norm', Float64, queue_size=1)
        self.param_pub = rospy.Publisher('param_est', Float64MultiArray,
            queue_size=1)
        self.cmd_timer = rospy.Timer(rospy.Duration(0.1),
            self.controllerCallback)

    def controllerReset(self):
        self.tau, self.F = np.zeros(3), np.zeros(3)
        self.o, self.g = np.zeros(4), np.zeros(2)
        self.d, self.c = np.zeros(4), np.zeros(4)


    def getParams(self):
        self.o_mags = np.fromstring(rospy.get_param('/ac/o_mags'), sep=", ")
        self.g_mags = np.fromstring(rospy.get_param('/ac/g_mags'), sep=", ")
        self.d_mags = np.fromstring(rospy.get_param('/ac/d_mags'), sep=", ")
        self.c_mags = np.fromstring(rospy.get_param('/ac/c_mags'), sep=", ")
        self.L_lin = rospy.get_param('/ac/L_lin')
        self.L_ang = rospy.get_param('/ac/L_ang')
        self.L = np.diag([self.L_lin,self.L_lin,self.L_ang])
        self.Kd_lin = rospy.get_param('/ac/Kd_lin')
        self.Kd_ang = rospy.get_param('/ac/Kd_ang')
        self.Kd = np.diag([self.Kd_lin,self.Kd_lin,self.Kd_ang])
        self.G_o = rospy.get_param('/ac/Gamma')*np.diag(self.o_mags)
        self.G_g = rospy.get_param('/ac/Gamma')*np.diag(self.g_mags)
        self.G_d = rospy.get_param('/ac/Gamma')*np.diag(self.d_mags)
        self.G_c = rospy.get_param('/ac/Gamma')*np.diag(self.c_mags)
        self.o_pos_elems = [0,1] #flags which elements to project to >0
        self.g_pos_elems = None
        self.d_pos_elems = [0,3]
        self.c_pos_elems = [0,3]
        self.deadband = rospy.get_param('/ac/deadband')
        self.q_filt = rospy.get_param('/ac/q_filt')
        self.dq_filt = rospy.get_param('/ac/dq_filt')
        self.offset_angle = rospy.get_param('offset_angle',0.) #angle offset
        self.moment_arm = np.fromstring(rospy.get_param('moment_arm'), sep=", ")
            #from payload frame, default to zero
        self.v_max = rospy.get_param('/ac/v_max',5.0)
        self.o[0] = rospy.get_param('/ac/m_init',15.)
        self.o[1] = rospy.get_param('/ac/J_init',15.)
        self.wrap_tol = rospy.get_param('/ac/wrap_tol',0.1)

    def activeCallback(self,msg):
        if not self.active and msg.data:
            self.getParams()
        elif self.active and not msg.data:
            self.controllerReset()

        self.active = msg.data

    def controllerCallback(self,event):
        """defines a timer callback to implement controller"""
        #define dynamics terms
        if self.state_time == -1:
            self.state_time = event.current_real.to_sec()
        else:
            #calculate state & current velocity
            dt = event.current_real.to_sec() - self.state_time
            if abs(self.q[2]-self.q_raw[2]) > 2*np.pi - self.wrap_tol:
                if self.q[2] > self.q_raw[2]:
                    self.q_raw[2] += 2*np.pi
                else:
                    self.q_raw[2] -= 2*np.pi
            q_smoothed = (1-self.q_filt)*self.q_raw + self.q_filt*self.q
            q_smoothed[2], self.q[2], self.q_prev[2] = self.wrap_angles(
                q_smoothed[2], self.q[2], self.q_prev[2])

            dq_new = (3*q_smoothed - 4*self.q + self.q_prev)/(2*dt)
            dq_new = np.clip(dq_new,-self.v_max,self.v_max)
            #dq_new = (q_new - self.q_prev)/dt

            self.q_prev = self.q
            self.q = q_smoothed
            self.dq = (1-self.dq_filt)*dq_new + self.dq_filt*self.dq

            #calculate local measurement using moment arm
            self.curr_arm = self.rot(self.q[-1])@self.moment_arm
            rix, riy = self.curr_arm[0:2]
            self.v_i = self.dq + np.array([-self.dq[-1]*riy,self.dq[-1]*rix,0.])
            #rospy.logwarn('i am actually doing this calculation')
            #rospy.logwarn(self.v_i)
            self.state_time= event.current_real.to_sec()

            if self.active:
                q_err = self.q - self.q_des
                if abs(q_err[2]) > np.pi:
                    #rospy.logwarn('this was a problem')
                    if q_err[2] > 0:
                        q_err -= 2*np.pi
                    else:
                        q_err += 2*np.pi
                dq_err = self.dq - self.dq_des
                s = dq_err + self.L@q_err
                dq_r = self.dq_des - self.L@q_err
                ddq_r = self.ddq_des - self.L@dq_err

                #control law
                self.F = (self.Y_o(dq_r,ddq_r) @ self.o + self.Y_d() @ self.d
                    + self.Y_c() @ self.c - self.Kd @ s) #world frame
                self.tau = self.Mhat_inv() @ self.F #world frame

                #adaptation law:
                if np.linalg.norm(s) > self.deadband:
                    #calculate param derivatives
                    do = -self.G_o@np.transpose(self.Y_o(dq_r,ddq_r))@s
                    dg = -self.G_g@np.transpose(self.Y_g())@s
                    dd = -self.G_d@np.transpose(self.Y_d(dq_r))@s
                    dc = -self.G_c@np.transpose(self.Y_c())@s
                    #apply derivatives
                    self.o = self.o + dt*do
                    self.g = self.g + dt*dg
                    self.d = self.d + dt*dd
                    self.c = self.c + dt*dc
                    #project onto feasible region
                    for param, elems in zip([self.o,self.g,self.d,self.c],
                        [self.o_pos_elems,self.g_pos_elems,self.d_pos_elems,
                        self.c_pos_elems]):
                        if elems is not None:
                            param[elems] = np.maximum(param[elems],0.)


                err_msg = Reference(Vector3(*q_err),Vector3(*dq_err),
                    Vector3(*s)) #use ddq field for s since it's empty otherwise
                self.err_pub.publish(err_msg)
                self.e_norm_pub.publish(Float64(np.linalg.norm(s)))

            #publish command in world frame; use force_global to rotate
            lin_cmd = Vector3(x=self.tau[0],y=self.tau[1],z=0.)
            ang_cmd = Vector3(x=0.,y=0.,z=self.tau[2])
            cmd_msg = Twist(linear=lin_cmd,angular=ang_cmd)
            self.cmd_pub.publish(cmd_msg)

        state_msg = Reference(Vector3(*self.q),Vector3(*self.dq),Vector3())
        self.state_pub.publish(state_msg)

        param_msg = Float64MultiArray()
        param_msg.data = np.concatenate((self.o,self.g,self.d,self.c),axis=0)
        self.param_pub.publish(param_msg)

    def stateCallback(self,data):
        '''handles measurement callback from Optitrack'''
        th = quaternion_to_angle(data.pose.orientation)
        q_new = np.array([data.pose.position.x,data.pose.position.y,th])
        self.q_raw = q_new

    def refCallback(self,data):
        self.q_des = np.array([data.q_des.x,data.q_des.y,data.q_des.z])
        self.dq_des = np.array([data.dq_des.x,data.dq_des.y,data.dq_des.z])
        self.ddq_des = np.array([data.ddq_des.x,data.ddq_des.y,data.ddq_des.z])

    def Mhat_inv(self):
        """defines correction term for moment arms in control law"""
        rhx, rhy = self.g
        _, _, th = self.q

        rhx_n = rhx*cos(th)+rhy*sin(th)
        rhy_n = -rhx*sin(th)+rhy*cos(th)

        return np.array([[1,0,0],[0,1,0],[rhy_n,-rhx_n,1]])

    def wrap_angles(self,z_new,z_curr,z_prev):
        if abs(z_new - z_curr) >= 2*np.pi - self.wrap_tol:
            print('wrapping!')
            z_new = z_new + 2*np.pi if z_new < z_curr else z_new - 2*np.pi

        if abs(z_curr - z_prev) >= 2*np.pi - self.wrap_tol:
            print('wrapping!')
            z_prev = z_prev + 2*np.pi if z_curr > z_prev else z_prev - 2*np.pi

        return z_new, z_curr, z_prev

    def Y_o(self,dqr,ddqr):
        x, y, th = self.q
        dx, dy, dth = self.dq
        dxr, dyr, dthr = dqr
        ddxr, ddyr, ddthr = ddqr
        block1h = np.array([[ddxr,0,-sin(th)*ddthr, cos(th)*ddthr],[ddyr,0,-cos(th)*ddthr,-sin(th)*ddthr],[0,ddthr,-sin(th)*ddxr-cos(th)*ddyr,cos(th)*ddxr-sin(th)*ddyr]])
        block1c = np.array([[0,0,dth*dthr*cos(th),dth*dthr*sin(th)],[0,0,-dth*dthr*sin(th),dth*dthr*cos(th)],[0,0,0,0]])
        Y = block1h + block1c
        return Y

    def Y_g(self):
        Fx,Fy = self.F[0:2]
        th = self.q[2]
        return np.array([[0,0],[0,0],[-Fy*cos(th)-Fx*sin(th),-Fy*sin(th)+Fx*cos(th)]])

    def Y_d(self,dq_r):
        x,y,th = self.q
        vx, vy, w = dq_r
        return np.array([[vx,w*sin(th),-w*cos(th),0],
                         [vy,w*cos(th),w*sin(th),0],
                         [0,vx*sin(th)+vy*cos(th),vy*sin(th)-vx*cos(th),w]])

    def Y_c(self):
        x,y,th = self.q
        eps = 1e-4
        sgn_v = self.v_i / (np.abs(self.v_i)+eps)
        vx, vy, w = sgn_v
        return np.array([[vx,0,0,0],[vy,0,0,0],
            [0,vx*sin(th)+vy*cos(th),vy*sin(th)-vx*cos(th),w]])

    def rot(self,t):
        return np.array([[cos(t),sin(t),0],[-sin(t),cos(t),0],[0,0,1]])

def quaternion_to_angle(q):
    """transforms quaternion to body angle in plane"""
    cos_th = 2*(q.x**2 + q.w**2)-1
    sin_th = -2*(q.x*q.y - q.z*q.w)
    return np.arctan2(sin_th,cos_th)

def main():
    rospy.init_node('hamilton_ac')
    try:
        AdaptiveController()
        rospy.logwarn('starting ac')
        rospy.spin()
    except rospy.ROSException as e:
        rospy.logwarn('closing ac')
        raise e

if __name__ == '__main__':
    main()
